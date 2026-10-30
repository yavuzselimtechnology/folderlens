(() => {
  "use strict";

  const decoder = new TextDecoder("utf-8", { fatal: false });
  const MAX_PACKS = 256;
  const MAX_PACK_INDEX_BYTES = 64 * 1024 * 1024;
  const MAX_OBJECT_BYTES = 16 * 1024 * 1024;
  const MAX_REFLOG_BYTES = 32 * 1024 * 1024;
  const MAX_REFLOG_RECORDS = 100_000;

  async function readGit(root, options = {}) {
    let git;
    try {
      git = await root.getDirectoryHandle(".git");
    } catch {
      return emptyGit();
    }

    const compareNames = options.compareNames || defaultCompare;
    const isSensitive = options.isSensitive || (() => false);
    const isIgnoredPath = options.isIgnoredPath || (() => false);
    const maxCommits = Math.max(1, Number(options.maxCommits) || 100_000);
    const head = (await readNestedText(git, "HEAD"))?.trim() || null;
    const headRef = head?.startsWith("ref: ") ? head.slice(5).trim() : null;
    const branch = headRef?.startsWith("refs/heads/")
      ? headRef.slice("refs/heads/".length)
      : headRef;
    const refs = await readRefs(git, compareNames);
    const packedRefs = await readNestedText(git, "packed-refs");
    if (packedRefs) parsePackedRefs(packedRefs, refs);
    if (head && /^[0-9a-f]{40,64}$/i.test(head)) {
      refs.push({ name: "HEAD", sha: head.toLowerCase() });
    }
    const orderedRefs = deduplicateRefs(refs).sort((left, right) =>
      compareNames(left.name, right.name)
    );

    const history = await readAllReflogs(git, compareNames);
    let trackedPaths = [];
    try {
      const indexHandle = await git.getFileHandle("index");
      trackedPaths = parseGitIndex(await (await indexHandle.getFile()).arrayBuffer());
    } catch {
      // Split, sparse, or unsupported indexes do not invalidate commit history.
    }

    let commitResult = {
      commits: [],
      complete: false,
      error: "Git object storage is unavailable.",
    };
    try {
      commitResult = await readCommitGraph(git, orderedRefs, maxCommits);
    } catch (error) {
      commitResult.error = error?.message || String(error);
    }

    return {
      detected: true,
      head,
      branch,
      history_source: "all readable .git/logs reflogs",
      refs: orderedRefs,
      history,
      tracked_paths: [...new Set(trackedPaths)]
        .filter((path) => !isSensitive(path) && !isIgnoredPath(path))
        .sort(compareNames),
      commit_graph_complete: commitResult.complete,
      commit_graph_source: "reachable commits from loose and packed Git objects",
      commit_graph_error: commitResult.error,
      commits: commitResult.commits,
    };
  }

  function emptyGit() {
    return {
      detected: false,
      head: null,
      branch: null,
      history_source: null,
      refs: [],
      history: [],
      tracked_paths: [],
      commit_graph_complete: false,
      commit_graph_source: null,
      commit_graph_error: "No Git repository was detected.",
      commits: [],
    };
  }

  async function readCommitGraph(git, refs, maximum) {
    if (typeof DecompressionStream === "undefined") {
      return {
        commits: [],
        complete: false,
        error: "This browser does not provide the deflate decoder required for Git objects.",
      };
    }
    const hashBytes = refs.some((item) => item.sha.length === 64) ? 32 : 20;
    const objects = await createObjectReader(git, hashBytes);
    const references = new Map();
    for (const ref of refs) {
      const names = references.get(ref.sha) || [];
      names.push(ref.name);
      references.set(ref.sha, names);
    }
    const queue = refs.map((item) => ({ sha: item.sha, references: [item.name] }));
    const queued = new Set(queue.map((item) => item.sha));
    const visited = new Set();
    const commitSeen = new Set();
    const commits = [];
    const failures = [];
    let cursor = 0;
    while (cursor < queue.length && commits.length < maximum) {
      const queuedItem = queue[cursor++];
      let sha = queuedItem.sha;
      if (visited.has(sha)) continue;
      visited.add(sha);
      try {
        let object = await objects.read(sha);
        let tagDepth = 0;
        while (object.type === "tag" && tagDepth < 16) {
          const target = parseTagTarget(object.data);
          if (!target) throw new Error(`Annotated tag ${sha} has no object target`);
          sha = target;
          object = await objects.read(sha);
          tagDepth += 1;
        }
        if (object.type !== "commit") continue;
        if (commitSeen.has(sha)) {
          const existing = commits.find((item) => item.sha === sha);
          if (existing) {
            existing.references = [...new Set([
              ...existing.references,
              ...queuedItem.references,
            ])].sort(defaultCompare);
          }
          continue;
        }
        commitSeen.add(sha);
        visited.add(sha);
        const commit = parseCommit(sha, object.data);
        commit.references = [...new Set([
          ...(references.get(sha) || []),
          ...queuedItem.references,
        ])].sort(defaultCompare);
        commits.push(commit);
        for (const parent of commit.parents) {
          if (!visited.has(parent) && !queued.has(parent)) {
            queue.push({ sha: parent, references: [] });
            queued.add(parent);
          }
        }
      } catch (error) {
        failures.push(`${sha}: ${error?.message || String(error)}`);
      }
    }
    commits.sort((left, right) => {
      const timeDifference =
        (right.committer?.timestamp || 0) - (left.committer?.timestamp || 0);
      return timeDifference || defaultCompare(left.sha, right.sha);
    });
    const capped = cursor < queue.length;
    return {
      commits,
      complete: !capped && failures.length === 0,
      error: capped
        ? `Commit graph exceeds the ${maximum.toLocaleString("en")} commit policy.`
        : failures.slice(0, 20).join("; ") || null,
    };
  }

  async function createObjectReader(git, hashBytes) {
    const objects = await git.getDirectoryHandle("objects");
    const packs = await readPackIndexes(objects, hashBytes);
    const cache = new Map();
    const offsetCache = new Map();

    function packedLocation(oid) {
      for (const pack of packs) {
        const offset = pack.index.offsetForOid(oid);
        if (offset !== null) return { pack, offset };
      }
      return null;
    }

    async function read(oid, depth = 0) {
      const normalized = String(oid).toLowerCase();
      if (!/^[0-9a-f]{40}$|^[0-9a-f]{64}$/.test(normalized)) {
        throw new Error("Invalid Git object id");
      }
      if (cache.has(normalized)) return cache.get(normalized);
      if (depth > 64) throw new Error("Git delta chain is too deep");
      let object = await readLooseObject(objects, normalized);
      if (!object) {
        const location = packedLocation(normalized);
        if (!location) throw new Error("Git object is missing or promised by a partial clone");
        object = await readPackedObject(location.pack, location.offset, depth);
      }
      cache.set(normalized, object);
      return object;
    }

    async function readPackedObject(pack, offset, depth) {
      if (depth > 64) throw new Error("Git delta chain is too deep");
      const key = `${pack.name}:${offset}`;
      if (offsetCache.has(key)) return offsetCache.get(key);
      const nextOffset = pack.index.nextOffset(offset);
      const file = pack.file;
      const bytes = new Uint8Array(
        await file.slice(offset, nextOffset).arrayBuffer()
      );
      const header = parsePackObjectHeader(bytes);
      let cursor = header.cursor;
      let base = null;
      if (header.type === 6) {
        const distance = readOfsDeltaDistance(bytes, cursor);
        cursor = distance.cursor;
        base = await readPackedObject(pack, offset - distance.value, depth + 1);
      } else if (header.type === 7) {
        if (cursor + hashBytes > bytes.length) throw new Error("Truncated ref delta");
        const baseOid = hex(bytes.slice(cursor, cursor + hashBytes));
        cursor += hashBytes;
        base = await read(baseOid, depth + 1);
      }
      const inflated = await inflate(bytes.slice(cursor));
      if (inflated.length !== header.size) {
        throw new Error("Packed Git object size mismatch");
      }
      let object;
      if (base) {
        const data = applyDelta(base.data, inflated);
        object = { type: base.type, data };
      } else {
        object = { type: packTypeName(header.type), data: inflated };
      }
      if (!object.type) throw new Error("Unsupported packed Git object type");
      if (object.data.length > MAX_OBJECT_BYTES) throw new Error("Git object is oversized");
      offsetCache.set(key, object);
      return object;
    }

    return { read };
  }

  async function readPackIndexes(objects, hashBytes) {
    let packDirectory;
    try {
      packDirectory = await objects.getDirectoryHandle("pack");
    } catch {
      return [];
    }
    const handles = new Map();
    for await (const entry of packDirectory.values()) {
      if (entry.kind === "file") handles.set(entry.name, entry);
    }
    const indexNames = [...handles.keys()]
      .filter((name) => /^pack-[0-9a-f]+\.idx$/i.test(name))
      .sort(defaultCompare)
      .slice(0, MAX_PACKS);
    const packs = [];
    for (const indexName of indexNames) {
      const packName = indexName.slice(0, -4) + ".pack";
      if (!handles.has(packName)) continue;
      const indexFile = await handles.get(indexName).getFile();
      const packFile = await handles.get(packName).getFile();
      if (indexFile.size > MAX_PACK_INDEX_BYTES || packFile.size < 32) continue;
      const index = parsePackIndex(
        await indexFile.arrayBuffer(), hashBytes, packFile.size
      );
      packs.push({
        name: packName,
        file: packFile,
        index,
      });
    }
    return packs;
  }

  function parsePackIndex(buffer, hashBytes, packSize) {
    const bytes = new Uint8Array(buffer);
    const view = new DataView(buffer);
    if (
      bytes.length < 8 + 1024 ||
      bytes[0] !== 0xff || bytes[1] !== 0x74 || bytes[2] !== 0x4f || bytes[3] !== 0x63 ||
      view.getUint32(4) !== 2
    ) {
      throw new Error("Only Git pack index v2 is supported");
    }
    const count = view.getUint32(8 + 255 * 4);
    const oidStart = 8 + 256 * 4;
    const crcStart = oidStart + count * hashBytes;
    const offsetStart = crcStart + count * 4;
    const largeStart = offsetStart + count * 4;
    if (largeStart > bytes.length - hashBytes * 2) throw new Error("Truncated Git pack index");
    const offsets = new Float64Array(count);
    for (let index = 0; index < count; index += 1) {
      const offset32 = view.getUint32(offsetStart + index * 4);
      let offset = offset32;
      if (offset32 & 0x80000000) {
        const largeIndex = offset32 & 0x7fffffff;
        const position = largeStart + largeIndex * 8;
        if (position + 8 > bytes.length) throw new Error("Invalid large Git pack offset");
        offset = Number(view.getBigUint64(position));
      }
      if (!Number.isSafeInteger(offset)) throw new Error("Git pack offset is too large");
      offsets[index] = offset;
    }
    const orderedOffsets = offsets.slice().sort();
    for (let index = 0; index < orderedOffsets.length; index += 1) {
      const next = index + 1 < orderedOffsets.length
        ? orderedOffsets[index + 1]
        : packSize - hashBytes;
      if (next <= orderedOffsets[index]) {
        throw new Error("Invalid Git pack object boundaries");
      }
    }

    return {
      count,
      offsetForOid(oid) {
        const target = hexBytes(oid, hashBytes);
        let low = 0;
        let high = count - 1;
        while (low <= high) {
          const middle = (low + high) >>> 1;
          const start = oidStart + middle * hashBytes;
          let comparison = 0;
          for (let index = 0; index < hashBytes; index += 1) {
            comparison = bytes[start + index] - target[index];
            if (comparison) break;
          }
          if (comparison === 0) return offsets[middle];
          if (comparison < 0) low = middle + 1;
          else high = middle - 1;
        }
        return null;
      },
      nextOffset(offset) {
        let low = 0;
        let high = orderedOffsets.length - 1;
        while (low <= high) {
          const middle = (low + high) >>> 1;
          const value = orderedOffsets[middle];
          if (value === offset) {
            return middle + 1 < orderedOffsets.length
              ? orderedOffsets[middle + 1]
              : packSize - hashBytes;
          }
          if (value < offset) low = middle + 1;
          else high = middle - 1;
        }
        throw new Error("Packed Git object offset is not indexed");
      },
    };
  }

  async function readLooseObject(objects, oid) {
    try {
      const directory = await objects.getDirectoryHandle(oid.slice(0, 2));
      const handle = await directory.getFileHandle(oid.slice(2));
      const file = await handle.getFile();
      if (file.size > MAX_OBJECT_BYTES) throw new Error("Loose Git object is oversized");
      const inflated = await inflate(new Uint8Array(await file.arrayBuffer()));
      const separator = inflated.indexOf(0);
      if (separator < 1) throw new Error("Invalid loose Git object header");
      const [type, sizeText] = decoder.decode(inflated.slice(0, separator)).split(" ");
      const data = inflated.slice(separator + 1);
      if (Number(sizeText) !== data.length) throw new Error("Loose Git object size mismatch");
      return { type, data };
    } catch (error) {
      if (error?.name === "NotFoundError") return null;
      throw error;
    }
  }

  async function inflate(bytes) {
    const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate"));
    const data = new Uint8Array(await new Response(stream).arrayBuffer());
    if (data.length > MAX_OBJECT_BYTES) throw new Error("Inflated Git object is oversized");
    return data;
  }

  function parsePackObjectHeader(bytes) {
    if (!bytes.length) throw new Error("Empty packed Git object");
    let cursor = 0;
    let byte = bytes[cursor++];
    const type = (byte >> 4) & 7;
    let size = byte & 0x0f;
    let shift = 4;
    while (byte & 0x80) {
      if (cursor >= bytes.length || shift > 56) throw new Error("Invalid Git pack header");
      byte = bytes[cursor++];
      size += (byte & 0x7f) * 2 ** shift;
      shift += 7;
    }
    if (size > MAX_OBJECT_BYTES) throw new Error("Packed Git object is oversized");
    return { type, size, cursor };
  }

  function readOfsDeltaDistance(bytes, start) {
    let cursor = start;
    if (cursor >= bytes.length) throw new Error("Truncated offset delta");
    let byte = bytes[cursor++];
    let value = byte & 0x7f;
    while (byte & 0x80) {
      if (cursor >= bytes.length) throw new Error("Truncated offset delta");
      byte = bytes[cursor++];
      value = ((value + 1) * 128) + (byte & 0x7f);
    }
    return { value, cursor };
  }

  function applyDelta(base, delta) {
    let cursor = 0;
    const baseSize = readDeltaVarint(delta, cursor);
    cursor = baseSize.cursor;
    if (baseSize.value !== base.length) throw new Error("Git delta base size mismatch");
    const resultSize = readDeltaVarint(delta, cursor);
    cursor = resultSize.cursor;
    if (resultSize.value > MAX_OBJECT_BYTES) throw new Error("Git delta result is oversized");
    const result = new Uint8Array(resultSize.value);
    let output = 0;
    while (cursor < delta.length) {
      const operation = delta[cursor++];
      if (operation & 0x80) {
        let offset = 0;
        let size = 0;
        if (operation & 0x01) offset |= delta[cursor++];
        if (operation & 0x02) offset |= delta[cursor++] << 8;
        if (operation & 0x04) offset |= delta[cursor++] << 16;
        if (operation & 0x08) offset += delta[cursor++] * 2 ** 24;
        if (operation & 0x10) size |= delta[cursor++];
        if (operation & 0x20) size |= delta[cursor++] << 8;
        if (operation & 0x40) size |= delta[cursor++] << 16;
        if (size === 0) size = 0x10000;
        if (offset + size > base.length || output + size > result.length) {
          throw new Error("Invalid Git delta copy instruction");
        }
        result.set(base.slice(offset, offset + size), output);
        output += size;
      } else {
        if (operation === 0 || cursor + operation > delta.length || output + operation > result.length) {
          throw new Error("Invalid Git delta insert instruction");
        }
        result.set(delta.slice(cursor, cursor + operation), output);
        cursor += operation;
        output += operation;
      }
    }
    if (output !== result.length) throw new Error("Git delta result size mismatch");
    return result;
  }

  function readDeltaVarint(bytes, start) {
    let cursor = start;
    let value = 0;
    let shift = 0;
    while (cursor < bytes.length) {
      const byte = bytes[cursor++];
      value += (byte & 0x7f) * 2 ** shift;
      if (!(byte & 0x80)) return { value, cursor };
      shift += 7;
      if (shift > 56) break;
    }
    throw new Error("Invalid Git delta size");
  }

  function packTypeName(type) {
    return ({ 1: "commit", 2: "tree", 3: "blob", 4: "tag" })[type] || null;
  }

  function parseCommit(sha, data) {
    let separator = -1;
    for (let index = 0; index + 1 < data.length; index += 1) {
      if (data[index] === 0x0a && data[index + 1] === 0x0a) {
        separator = index;
        break;
      }
    }
    const headerBytes = separator >= 0 ? data.slice(0, separator) : data;
    const headerText = decoder.decode(headerBytes);
    const headers = parseObjectHeaders(headerText);
    let messageDecoder = decoder;
    if (headers.encoding?.[0]) {
      try {
        messageDecoder = new TextDecoder(headers.encoding[0], { fatal: false });
      } catch {
        // Unknown legacy encodings remain readable with replacement characters.
      }
    }
    const message = separator >= 0
      ? messageDecoder.decode(data.slice(separator + 2))
      : "";
    const subject = message.split(/\r?\n/, 1)[0] || "";
    return {
      sha,
      tree: headers.tree?.[0] || null,
      parents: headers.parent || [],
      author: parseIdentity(headers.author?.[0]),
      committer: parseIdentity(headers.committer?.[0]),
      subject,
      message,
      references: [],
    };
  }

  function parseObjectHeaders(text) {
    const result = {};
    let current = null;
    for (const line of text.split("\n")) {
      if (/^[ \t]/.test(line) && current) continue;
      const space = line.indexOf(" ");
      if (space < 1) continue;
      current = line.slice(0, space);
      (result[current] ||= []).push(line.slice(space + 1));
    }
    return result;
  }

  function parseIdentity(value) {
    if (!value) return null;
    const match = value.match(/^(.*?) <([^>]*)> (\d+) ([+-]\d{4})$/);
    if (!match) return { name: value, email: "", timestamp: null, timezone: "" };
    return {
      name: match[1],
      email: match[2],
      timestamp: Number(match[3]),
      timezone: match[4],
    };
  }

  function parseTagTarget(data) {
    const match = decoder.decode(data).match(/^object ([0-9a-f]{40,64})$/m);
    return match?.[1]?.toLowerCase() || null;
  }

  async function readRefs(git, compareNames) {
    const records = [];
    let refsRoot;
    try {
      refsRoot = await git.getDirectoryHandle("refs");
    } catch {
      return records;
    }
    async function walk(directory, prefix) {
      const entries = [];
      for await (const entry of directory.values()) entries.push(entry);
      entries.sort((left, right) => compareNames(left.name, right.name));
      for (const entry of entries) {
        const path = `${prefix}${entry.name}`;
        if (entry.kind === "directory") await walk(entry, `${path}/`);
        else {
          const sha = (await (await entry.getFile()).text()).trim().toLowerCase();
          if (/^[0-9a-f]{40}$|^[0-9a-f]{64}$/.test(sha)) {
            records.push({ name: `refs/${path}`, sha });
          }
        }
      }
    }
    await walk(refsRoot, "");
    return records;
  }

  function parsePackedRefs(value, refs) {
    for (const line of value.split(/\r?\n/)) {
      if (!line || line.startsWith("#") || line.startsWith("^")) continue;
      const [sha, name] = line.split(" ");
      if (/^[0-9a-f]{40}$|^[0-9a-f]{64}$/i.test(sha || "") && name) {
        refs.push({ name, sha: sha.toLowerCase() });
      }
    }
  }

  async function readAllReflogs(git, compareNames) {
    let logs;
    try {
      logs = await git.getDirectoryHandle("logs");
    } catch {
      return [];
    }
    const records = [];
    let bytesRead = 0;
    async function walk(directory, prefix) {
      const entries = [];
      for await (const entry of directory.values()) entries.push(entry);
      entries.sort((left, right) => compareNames(left.name, right.name));
      for (const entry of entries) {
        if (records.length >= MAX_REFLOG_RECORDS || bytesRead >= MAX_REFLOG_BYTES) return;
        const path = `${prefix}${entry.name}`;
        if (entry.kind === "directory") await walk(entry, `${path}/`);
        else {
          const file = await entry.getFile();
          if (bytesRead + file.size > MAX_REFLOG_BYTES) continue;
          bytesRead += file.size;
          parseReflog(await file.text(), `logs/${path}`, records);
        }
      }
    }
    await walk(logs, "");
    return records.slice(0, MAX_REFLOG_RECORDS).map((record, index) => ({
      ...record,
      sequence: index + 1,
    }));
  }

  function parseReflog(value, source = "logs/HEAD", target = []) {
    for (const line of value.split(/\r?\n/)) {
      if (!line) continue;
      const match = line.match(
        /^([0-9a-f]{40,64}) ([0-9a-f]{40,64}) (.*?) <([^>]*)> (\d+) ([+-]\d{4})\t(.*)$/i
      );
      if (!match) continue;
      target.push({
        sequence: target.length + 1,
        source,
        old_sha: match[1].toLowerCase(),
        new_sha: match[2].toLowerCase(),
        actor: match[3],
        email: match[4],
        timestamp: Number(match[5]),
        timezone: match[6],
        message: match[7],
      });
    }
    return target;
  }

  function parseGitIndex(buffer) {
    const bytes = new Uint8Array(buffer);
    const view = new DataView(buffer);
    if (bytes.length < 12 || decoder.decode(bytes.slice(0, 4)) !== "DIRC") return [];
    const version = view.getUint32(4);
    if (![2, 3].includes(version)) return [];
    const entryCount = Math.min(view.getUint32(8), 1_000_000);
    const paths = [];
    let offset = 12;
    for (let index = 0; index < entryCount && offset + 62 <= bytes.length; index += 1) {
      const entryStart = offset;
      const flags = view.getUint16(offset + 60);
      const declaredLength = flags & 0x0fff;
      // Version 3 entries flagged "extended" carry two more flag bytes.
      offset += version >= 3 && (flags & 0x4000) ? 64 : 62;
      let end = offset;
      const maximum = Math.min(bytes.length, offset + 4096);
      if (declaredLength < 0x0fff) end = Math.min(offset + declaredLength, maximum);
      else while (end < maximum && bytes[end] !== 0) end += 1;
      const path = decoder.decode(bytes.slice(offset, end));
      if (path) paths.push(path);
      while (end < bytes.length && bytes[end] !== 0) end += 1;
      offset = end + 1;
      while ((offset - entryStart) % 8 !== 0) offset += 1;
    }
    return paths;
  }

  async function readNestedText(root, path) {
    try {
      const parts = path.split("/");
      let directory = root;
      for (const part of parts.slice(0, -1)) {
        directory = await directory.getDirectoryHandle(part);
      }
      const handle = await directory.getFileHandle(parts.at(-1));
      return (await handle.getFile()).text();
    } catch {
      return null;
    }
  }

  function deduplicateRefs(refs) {
    const values = new Map();
    for (const ref of refs) values.set(ref.name, ref);
    return [...values.values()];
  }

  function hex(bytes) {
    return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  function hexBytes(value, expectedBytes) {
    if (value.length !== expectedBytes * 2 || !/^[0-9a-f]+$/.test(value)) {
      throw new Error("Invalid Git object id");
    }
    const bytes = new Uint8Array(expectedBytes);
    for (let index = 0; index < expectedBytes; index += 1) {
      bytes[index] = Number.parseInt(value.slice(index * 2, index * 2 + 2), 16);
    }
    return bytes;
  }

  function defaultCompare(left, right) {
    return String(left).localeCompare(String(right), "en", {
      numeric: true,
      sensitivity: "variant",
    });
  }

  window.AgentArmGit = {
    readGit,
    _test: {
      applyDelta,
      parseCommit,
      parseGitIndex,
      parsePackIndex,
      parseReflog,
    },
  };
})();
