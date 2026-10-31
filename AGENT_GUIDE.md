# FolderLens Agent

FolderLens'in yerel ajan modu, ChatGPT veya uyumlu bir MCP istemcisine bilgisayardaki
bir çalışma klasörü üzerinde araçlar sağlar. Sohbet modeli işi planlar ve araçları
çağırır; FolderLens modeli taklit etmez, ChatGPT oturum çerezlerini okumaz ve API
anahtarı gerektirmez. ChatGPT hesabı, model erişimi ve araç izinleri istemciye bağlıdır.

## Başlatma ve ChatGPT masaüstü bağlantısı

Python 3.11+ gerekir. PowerShell'de:

```powershell
cd C:\FolderLens
python folderlens.py --doctor
python folderlens.py --root "C:\calisma\projem" --allow-write --allow-exec --print-config toml
```

İlk komut eksik bağımlılıkları projenin `.venv` ortamına kurar ve araç kaydını denetler.
İkinci komutta gerçekten yetki vermek istediğiniz mevcut klasörü seçin. Yazdırılan
`[mcp_servers.folderlens]` bölümünü istemcinin MCP yapılandırmasına ekleyin. Komut
mevcut ayarlarınızı değiştirmez ve herhangi bir sunucuya veri yüklemez.

2026-10-30 tarihinde açık ChatGPT uygulamasında doğrulanan yol **Settings → Plugins →
MCPs → Add → Add MCP server → STDIO**: çıktıdaki `command` Python yürütülebiliridir,
`args` ise argüman listesidir. Aynı form Streamable HTTP türünü de destekler.
Kaydetme sonrasında istemcinin bağlantı durumunu ve `get_config` sonucunu kontrol edin. Menü/erişim hesap ve
istemci sürümüne bağlı olabilir. Aynı makinedeki Codex istemcileri ortak MCP
yapılandırmasını kullanabilir. JSON biçimini kullanan diğer istemciler için
`--print-config json` vardır.

STDIO'da istemci sunucuyu kendisi başlatır. Ayrı bir terminalde elle başlatılan
STDIO sürecine URL yapıştırarak bağlanılmaz. Elle çalıştırırsanız süreç stdin'den
MCP mesajları bekler; kullanıcıya ait bir sohbet giriş ekranı açmaz.

Birden çok çalışma klasörü veya bilgisayarı istemcide ayırt etmek için
`--name work-laptop` gibi farklı adlar kullanın. `--print-config` ilgili adla ayrı
bir MCP yapılandırma bölümü üretir. Her cihazdaki FolderLens kendi yetkileri ve
tokenıyla bağımsız çalışır. Varsayılan `list_devices` yalnız çağrıldığı sunucuyu tanımlar;
isteğe bağlı sahip yapılandırması aşağıdaki ek cihaz kataloğunu açar.
Bu, otomatik bulut cihaz keşfi veya başka bilgisayara kendiliğinden kurulum değildir.

### Sahip tarafından tanımlanan cihaz kataloğu

`--devices C:\ozel\devices.json` ile en çok 16 ek FolderLens HTTP sunucusunu
tanımlayabilirsiniz. Dosya en çok 64 KiB ve aşağıdaki sürümde olmalıdır:

```json
{
  "version": 1,
  "devices": [{
    "id": "work-laptop",
    "url": "https://mcp.example.com/mcp",
    "root": "C:\\calisma\\projem",
    "token_env": "FOLDERLENS_PEER_LAPTOP_TOKEN",
    "allow_write": false,
    "allow_exec": false
  }]
}
```

Kimlik ve kök, hedefin `get_config` sonucuyla birebir eşleşmelidir. HTTPS gerekir;
yerel testte yalnız literal loopback IP ile HTTP kabul edilir. Ham erişim anahtarları
JSON'a yazılmaz: sahibi ilgili ortam değişkenine hedefin bearer anahtarını koyar.
Bu değer araç yanıtlarına girmez ve yönetilen komut ortamından süzülür.
Yapılandırma dosyası modelin dosya araçlarından korunur; değişiklik için yerelde
yeniden başlatma gerekir. Katalog yetkileri OAuth politika bağına katılır ve onay
terminalinde gösterilir; değiştirmek eski yetkilendirmeleri geçersiz kılar.

Başlangıç ve normal `list_devices` ağ isteği yapmaz. `list_devices(refresh=true)`
hedefleri sırayla initialize/get_config ile denetler; cihaz başına 3 saniye, toplam
12 saniye ve yanıt başına 64 KiB sınırı vardır. Yönlendirme/sıkıştırma reddedilir;
kimlik doğrulama, kök/kimlik uyuşmazlığı ve erişilememe ayrı durumlar olarak döner.
Durum 60 saniye sonra eskidir; izinler yalnız güncel doğrulama, hedef politikası,
profil tavanı ve mevcut çağıranın kapsamları kesiştiğinde görünür. `next_offset`
ile devam edilir.

`call_device(device_id, tool_name, arguments, timeout_seconds=30)` seçilen hedefte
tek araç çağırır. Örneğin `device_id: "work-laptop"`, `tool_name: "read_file"`,
`arguments: {"path": "README.md"}`. Her çağrıda aynı MCP oturumunda hedefin kimliği,
kökü ve izinleri yeniden doğrulanır; önbellekteki online durumu yetki sayılmaz.
Hedef aracın okuma/yazma/exec kapsamı, çağıranın güncel OAuth yetkisi, bu sunucunun
yerel politikası ve profil tavanı birlikte uygulanır. Profilde `allow_exec` komutları
hedefin OS kullanıcısıyla çalıştırır; çalışma klasörü komutlar için sandbox değildir.

Yönlendirme yalnız bu sürümde kayıtlı normal araçlara açıktır. İç içe hub çağrıları,
cihaz kataloğu, geçmiş/istatistik, uzak iş kontrol araçları ve `shutdown` ayrı hedef
bağlantısından kullanılır. Böylece güçlü hedef anahtarı üzerinden daha dar kapsamlı
çağırana ayrıcalıklı geçmiş veya iş sonucu sızmaz. Hub `call_device` argüman/sonuçlarını
kalıcı içerik geçmişine yazmaz; hedef kendi geçmiş politikasını uygular.
Native metin/görsel içerik korunur; hedefin OAuth `_meta` alanı hub kimlik doğrulaması
gibi aktarılmaz. Ağ yanıtı 8 MiB, argüman JSON'u 32 MiB; toplam çağrı süresi 1..120
saniye (varsayılan 30) ile sınırlıdır.

İşlem öncesi hata `not_forwarded` döner. İstek gönderildikten sonra yanıt alınamazsa
`outcome_unknown` döner: işlem gerçekleşmiş olabilir; gerçek hedef durumunu inceleyin.
Otomatik yeniden gönderim yoktur. Uzayan işler için hub'da `start_tool_call` ile
`tool_name: "call_device"` ve benzersiz request_id kullanıp `get_tool_result` okuyun;
aynı request_id saklama süresince ikinci hedef işlemi oluşturmaz. Tamamlanmış sonuç
ilk çağrının bütün etkin kapsamlarına bağlı kalır. Doğrudan `call_device` çağrısının
kendi kalıcı idempotency garantisi yoktur.

Alternatif giriş noktaları:

```powershell
python run.py --agent --root "C:\calisma\projem" --allow-write --allow-exec
.\start.ps1 -Agent -Workspace "C:\calisma\projem" -AllowWrite -AllowExec
```

`python run.py` eski web uygulamasını açmaya; `python run.py --server` eski yerel
paylaşım sunucusunu çalıştırmaya devam eder. Ajan sunucusu web uygulamasına
import edilmez; halka açık `/AgentArm/` rotaları terminal yetkisi kazanmaz.

## Yetkiler ve kurtarma

`who_am_i`, seçilen cihaz adını/hostname'ini, sunucu PID/platformunu, ortam
değişkeninden alınmayan gerçek OS süreç sahibini ve mevcut OAuth bağlantısının
istemci kimliği/kapsamlarını gösterir. Etkin yazma/komut izinleri yerel politika
ile token kapsamının kesişimidir. Token'ın kendisi, ortam değişkenleri ve
ChatGPT hesap/abonelik bilgisi döndürülmez; OS hesabı ChatGPT'de oturum açan
insanın kimliği anlamına gelmez. OS sahibi okunamazsa hesap `null` ve hata türü
açıkça bildirilir; yetki yükseltme denenmez.

### İsteğe bağlı kalıcı araç geçmişi

Varsayılan `--history memory`, son 200 çağrının yalnız ad/süre/başarı/zaman
bilgisini RAM'de tutar. `--history metadata` aynı tür kayıtları yeniden başlatma
sonrasında da korur. `--history content` ayrıca argüman ve sonuçların ayrı ayrı
6000 karakterlik, JSON olarak kodlanmış metin önekini saklar; `truncated` alanı
kesilmeyi belirtir. Bu mod komutları, dosya içeriğini ve hassas verileri
saklayabilir; otomatik gizli bilgi temizliği veya şifreleme vaat edilmez.
Native görsel/medya binary verisi kaydedilmez. Bu seçimi yalnız yerel başlatma
komutuna veya `--print-config` üretimine ekleyebilirsiniz; sohbetten açılamaz.

Kayıtlar kurulumun `.folderlens/history/<cihaz>/<politika-hash>/` dizinindeki
özel SQLite veritabanındadır; kullanıcıya özel izinler uygulanır ve dosya
araçlarından erişim engellenir. Kök/yazma/komut/URL politikası değişirse ayrı
geçmiş kullanılır. En fazla 200 kayıt, 8 MiB serileştirilmiş kayıt yükü ve 30 gün
saklanır; süre temizliği başlangıçta ve sonraki okuma/yazmalarda yapılır.
SQLite veritabanı için ayrıca 16 MiB tavan vardır; geçici transaction journal'ı
ayrı disk alanı kullanabilir. Silinen içerik güvenli SQLite silmesiyle temizlenir;
disk/işletim sistemi yedeklerinde adli silme garantisi verilmez.

`get_recent_tool_calls(limit=20)` en yeni pencereyi kronolojik sırada döndürür.
`has_more` varsa `next_before_id` değerini `before_id` olarak vererek daha eski
kayıtlara geçin. `memory` modunda bu imleç yoktur. Her yanıt çıktı bütçesiyle
sınırlıdır. Yazma/komut kapsamlı kayıtlar etkin yerel izinlere ve varsa mevcut
OAuth token kapsamlarına göre gizlenir. `metadata` moduna dönmek daha önce
saklanmış içerik alanlarını da araç yanıtından çıkarır; diskteki eski içerik
kayıt sınırı/süre temizliğine kadar kalabilir.

Geçmiş okuyucusu ve kullanım istatistiği kendi sonuçlarını kalıcı kaydetmez.
Kayıt hatası başarılı dosya değişikliğini başarısız göstermez; son depolama hata
türü `get_usage_stats.history_last_error` içinde görünür. Kullanım sayaçları
çalışan sürece aittir; eski çağrılar yeniden çalıştırılmaz ve undo kayıtları
geçmişten geri yüklenmez.

- Seçenek verilmezse dosya araçları salt okunurdur. Okuma, arama, Git inceleme ve
  önizleme çalışır. `--allow-write` dosya/klasör değişikliklerini açar.
- Çalışan FolderLens kurulum dizini (Python import kökü) ve kullanılan Python
  ortamları dosya araçlarıyla değiştirilemez; sınırlar `get_config` içindeki
  `runtime_read_only_paths` alanında görünür. Yeni bir gölge Python modülü
  oluşturma, üst klasörü taşıma ve undo da aynı denetime tabidir. Böylece yazma
  yetkisi, belge helper'ının kodunu değiştirerek komut yetkisine dönüşmez.
  Proje/veri çalışma kökünü FolderLens kurulumunun dışında seçin. Kurulumun
  kendisini geliştirmek için sahibi tarafından açılmış komut yetkisi ve açık
  komut çalıştırma akışı gerekir; `--allow-exec` dosya araçlarındaki bu sınırı kaldırmaz.
  Okuma, önizleme ve kurulumdan dışarı dosya kopyalama kullanılabilir.
- `--allow-exec`, komutları işletim sistemi kullanıcısının yetkileriyle çalıştırır.
  Klasör sınırı ve gizli dosya filtresi komutları hapsetmez. Yalnızca güvendiğiniz
  model/istemciye verin. Çocuk süreçlerin ortamından TOKEN/SECRET/PASSWORD/API_KEY
  adlarını içeren değişkenler çıkartılır; bu, bir komutun diskten gizli bilgi
  okumasını engelleyen bir güvenlik sınırı değildir.
- Dosya yolları `/` ayraçlı, köke göre göreli olmalıdır. Üst dizin geçişleri,
  Windows alternatif veri akışları, symlink/junction/reparse noktaları, `.git`,
  gerçek `.env` ve özel anahtar dosyaları, üretilmiş klasörler reddedilir.
- Araçların dosya filtresi bir işletim sistemi sandbox'ı değildir. Kötü niyetli
  başka bir yerel süreçle aynı klasörü paylaşmayın; denetim ve dosya değiştirme
  arasındaki işletim sistemi yarışlarını bütünüyle engellemez.
- Yazmada güncel `base_sha256` zorunludur. `null` yalnız yeni dosya içindir.
  `write_file` ve `edit_block` varsayılan olarak yalnız önizleme üretir;
  uygulamak için `preview: false` kullanılır. `edit_block` eşleşme sayısını ve
  orijinal kodlamayı korur. Metin doğrudan yazılırsa UTF-8 kullanılır.
- Yazmalar geçici komşu dosya ve atomik rename ile tamamlanır. Bütün dosya seti
  için transaction değildir. Başarılı işlemde dönen `undo_id` geri alma içindir;
  sonraki dış değişiklikle çatışırsa geri alma reddedilir.
- Son 20 işlem ve toplam 16 MiB önceki içerik RAM'de saklanır. Eski kayıtlar
  bütçe dolunca çıkarılır; yeniden başlatmada kaybolur. Silme yalnız tek dosya
  içindir, klasör ağacı silinmez. Taşıma iki geri alma kimliği döndürür: önce
  `source_undo_id`, sonra `undo_id` uygulanır.
- Klasör taşımada önce `get_file_info(path, inspect_tree: true)` ile ağaç hash'i
  alın; sonra `move_file` çağrısına bu `sha256` değerini verin. İçerik, göreli
  adlar, boş dizinler, dosya zamanları ve mode bitleri denetlenir. Eksik tarama,
  korunan/üretilmiş dosya, symlink/junction veya özel aygıt girdisi içeren ağaç
  taşınmaz. Toplam içerik etkin `max_file_bytes` (varsayılan 16 MiB), manifest
  `max_undo_bytes`, kök dahil girdi sayısı `max_scan_files` ile sınırlıdır.
  Girdiler/okuma parçaları arasında 10 saniyelik tarama bütçesi denetlenir;
  bloklanan işletim sistemi dosya çağrıları bu denetimden ayrı kalabilir.
  Tarama sonunda yakalanan tüm girdilerin kimlik/mod/zaman/boyut bilgileri yeniden
  denetlenir. Bu, başka süreçlerle paylaşılan dosya sisteminin atomik anlık
  görüntüsü değildir; genel yerel yarış sınırları geçerlidir.
  Aynı dosya sistemindeki rename hedefi atomik olarak değiştirmeden taşır:
  Windows rename, Linux `renameat2(RENAME_NOREPLACE)` gerekir. Desteklenmeyen
  sistemde korumasız rename'e düşülmez. Windows yolu bu makinede test edildi;
  Linux uygulaması yerel Windows testinin kanıt kapsamında değildir.
  Klasör taşıma tek `undo_id` döndürür; ağaç sonradan değişmişse veya eski konum
  yeniden oluşturulmuşsa geri alma reddedilir. İçerik kopyası tutulmadığından
  backup byte bütçesi harcanmaz, fakat aynı 20 kayıt sınırı geçerlidir. Kaydı
  kurtarmayı engelleyecek byte/girdi bütçesi daraltmaları kabul edilmez.
- Sunucuyu Ctrl+C ile kapatın veya istemciden devre dışı bırakın. Yönetilen
  süreçler kapanışta sonlandırılır. Dosya işlemlerini geri almak için kapanıştan
  önce `undo_change` kullanın; kalıcı çalışma için Git commit'i kullanın.
- `set_config_value` çalışma sırasında yetkiyi yalnız daraltabilir: `allow_write`
  ve `allow_exec` için yalnız `false`; `max_file_bytes` ve `max_scan_files` için
  yalnız mevcut değerden küçük/eşit pozitif tamsayı; `url_hosts` için yalnız
  mevcut izin listesinin alt kümesi kabul edilir. Komut yetkisini kapatmak açık
  oturumları da sonlandırır. Yeni host/kök veya yetki eklemek için yerel yeniden
  başlatma gerekir. Dosya bütçesini mevcut geri almayı bozan düzeye indirmek
  reddedilir. Bu ayarlar diske yazılmaz.

## HTTP bağlantısı

STDIO yerine ayrı terminalde çalışan yerel HTTP sunucusu kullanılabilir:

```powershell
$env:FOLDERLENS_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
python folderlens.py --root "C:\calisma\projem" --transport http --allow-write --allow-exec
```

Adres `http://127.0.0.1:8766/mcp` olur. Her HTTP isteğinde
`Authorization: Bearer <FOLDERLENS_TOKEN>` zorunludur. Tokenı sohbete veya Git'e
yazmayın. İstemcinin kendi ortamında aynı değişkeni tanımlayın ve `--transport http
--print-config toml` çıktısını kullanın. Doktor denetimi HTTP'de tokenı da doğrular.
Sunucu yalnız loopback dinler; CORS açmaz, Host/Origin denetimini kapatmaz ve tokenı
URL parametresiyle kabul etmez. `--public-origin https://alanadiniz` yalnız açıkça
seçilmiş tünel/proxy origin'ini izin listesine ekler; kendisi tünel/deploy yapmaz.

ChatGPT web yerel `config.toml` okumaz. Web bağlantısı için hesabın desteklediği
MCP plugin bağlantısı, uygun kimlik doğrulaması ve erişilebilir güvenli taşıma
gereklidir. `--auth oauth` tek sahipli OAuth bağlantısını, terminalde onayı,
PKCE ve ayrı okuma/yazma/komut izinlerini sağlar. Erişilebilir HTTPS origin'i ve
istemcinin callback adresi ayrıca yapılandırılır. Adımlar, istemci iptali ve
özel ağ seçeneği için [TR/EN uzak bağlantı rehberini](https://yavuzselimtech.tr/folderlens/agent/?lang=tr#web) izleyin.
Sunucu kendiliğinden tünel açmaz veya deploy yapmaz; çok cihazlı bulut hesabı sunmaz.

## Araçlar

| Grup | Araçlar ve davranış |
| --- | --- |
| Keşif ve cihaz seçimi | `get_config`, `who_am_i`, `get_file_info`, `list_directory`, `list_devices`, `call_device`; izinler, bütçeler, cihaz/OS süreç sahibi/bağlantı kimliği, zaman/mode/metin satırları, dosya/ağaç SHA-256, sayfalı liste ve açık hedefte doğrulanmış araç çağrısı |
| Yapılandırma | `set_config_value`; yetki/bütçe/URL izinlerini yalnız daraltma; sohbetten yetki genişletme yok |
| Okuma | `read_file`, `read_lines`, `read_multiple_files`, `read_image`; karakter/satır penceresi, tail, belge çıkarımı, özgün resim içeriği |
| URL | `read_url`; yerelde izin verilen HTTPS hostlarından sayfalı metin/PDF okuma, DNS/IP ve yönlendirme denetimi |
| Değişiklik | `preview_changes` (salt okunur diff), `write_file`, `edit_block`, `create_directory`, `copy_file`, `move_file`, `delete_file`, `undo_change` |
| Arama | `start_search`, `get_more_search_results`, `list_searches`, `stop_search`; literal/regex metin veya dosya yolu, dosya adı glob, bağlam, erken durdurma |
| Terminal | `start_process`, `interact_with_process`, `read_process_output`, `force_terminate`, `list_sessions` |
| Sistem | `get_processes`; PID/ad/durum/bellek/CPU ve süreç oluşturulma kimliği; `kill_process` için kesin kimlik, komut yetkisi ve OS hakları gerekir; komut satırı/kimlik bilgisi döndürülmez |
| Belgeler | `list_workbook_sheets`, `read_workbook`, `write_workbook`, `write_document`, `read_word`, `edit_word`, `edit_word_xml`, `edit_pdf`; Excel sayfaları/hücreleri, belge oluşturma, Word metin/XML ve PDF sayfa düzenleme |
| Zengin PDF | `write_pdf`, `render_pdf_page`; Markdown/HTML/CSS, tablo, kod, SVG, yerel görsel, A4/Letter ve gerçek sayfa önizlemesi |
| PDF okuma | `read_pdf`; sayfa bazlı metin pencereleri, gömülü görsel kataloğu ve native görsel çıkarma |
| Git | `git_inspect`: status, tüm ref'lerde log, dosyaya daraltılmış diff/show; push/commit yapmaz |
| Denetim | `get_recent_tool_calls`, `get_usage_stats`; varsayılan içeriksiz RAM kaydı, isteğe bağlı kalıcı metadata/içerik, kapsam ve boyut sınırları |
| Uzayan çağrılar | `start_tool_call`, `get_tool_result`, `list_remote_tool_calls`; istemci istek kimliğiyle tekilleştirme, durum/sonuç, native içerik ve sayfalı katalog |
| Erişimi kapatma | `shutdown(device_id)`; kullanıcının açık bağlantı kesme talebinde doğru cihaz/exec kapsamıyla HTTP sunucusunu yanıt sonrasında kapatma |

## HTTP uzak erişimini kapatma

Yalnız kullanıcı bu cihazın uzak erişimini kapatmanızı açıkça istediğinde
`shutdown(device_id)` çağırın. Kimlik `get_config.device_id` ile aynı olmalıdır.
Sunucu `--allow-exec` ile başlatılmış olmalı; OAuth bağlantısı ayrıca
`folderlens:exec` kapsamını taşımalıdır. `get_config.remote_shutdown_available`
mevcut bağlantı için kullanılabilirliği gösterir. İşlem yalnız doğrudan HTTP
çağrısında çalışır; `start_tool_call` üzerinden veya STDIO'dan başlatılamaz.
STDIO süreçlerini istemci yönetir; yerelde Ctrl+C de kullanılabilir.

Başarılı yanıt, isteğin kabul edildiğini bildirir. Son HTTP yanıt gövdesi ASGI
gönderim aşamasını tamamladıktan sonra sunucu kapanış sinyali alır; ağın karşı
uçta yanıtı okuduğu garanti edilmez. Yeni doğrudan araç işlemleri bu aşamadan
sonra reddedilir. Çalışmaya başlamış doğrudan işlemler biter, kabul edilmiş
arka plan çağrıları tamamlanır; ardından sahip olunan terminal süreç ağaçları
sonlandırılır ve özel depolar kapatılır. Yanıt işaretleri bu davranışı da açıklar.

Bu işlem yalnız seçili FolderLens HTTP sunucusunu kapatır; genel bilgisayarı,
paylaşım sunucusunu veya diğer uygulamaları kapatmaz. Tek bir komutu durdurmak
için `force_terminate`/`kill_process` kullanın. Erişimi geri açmak için sahibi
aynı terminal komutunu yerelde yeniden çalıştırmalıdır. Kayıtlı OAuth bağlantı
onayları bu işlemle silinmez; kalıcı iptal gerekiyorsa yerel istemci iptal
komutunu kullanın. Kapanışta RAM'deki çağrı sonuçları ve undo kayıtları kaybolur.

## Uzayan araç çağrıları

Bir mevcut aracı normal çağırmak yerine `start_tool_call` ile başlatabilirsiniz:

```json
{
  "tool_name": "write_pdf",
  "arguments": {
    "path": "rapor.pdf",
    "content": "# Rapor\nDoğrulanmış çalışma sonucu.",
    "base_sha256": null
  },
  "request_id": "her-yeni-islem-icin-yeni-bir-UUID"
}
```

`arguments`, hedef aracın normal sözleşmesiyle resmî MCP SDK tarafından
doğrulanır. Yazma/komut yetkisi bu yöntemle açılmaz; kök/hash/izin ve OAuth
kuralları aynen geçerlidir. Üç çağrı kontrol aracı kendisini veya birbirini
arka planda çağıramaz. `request_id` 1..128 boşluksuz yazdırılabilir ASCII
karakter olmalı; yeni mantıksal işlem için yeni bir UUID kullanın. Dönen
`call_id` ile `get_tool_result(call_id, wait_ms=0)` çağırın. İsteğe bağlı bekleme
0..30000 ms'dir; bitiş bildirimiyle uyanır. Durum `pending`, `executing`,
`completed` veya `failed` olabilir. Asıl işlemler mevcut tek kilitte sırayla
çalışır; bu üç kontrol aracı o kilidi beklemez.

Ağ bağlantısı ilk yanıt gelmeden kesilirse **aynı araç/argüman/request_id** ile
başlatma çağrısını yineleyin. Kayıt saklandığı sürece aynı `call_id` döner ve
yeni işlem çalışmaz. Aynı anahtarla farklı argümanlar reddedilir. Çalışan işi
aynı argümanlarla doğrudan veya yeni anahtarla yeniden başlatmayın. Katalog
işlem kaydını tutar; bir hata, kısmi yan etki olmadığının garantisi değildir.

Sonuç yalnız başlatan OAuth istemcisine ve başlatma anındaki **bütün etkin
kapsamları** hâlâ taşıyan bağlantıya açılır. Bu bağ, örneğin yüksek yetkiyle
okunan araç geçmişinin daha dar bir token üzerinden tekrar okunmasını önler.
Sonradan izin daraltma eski sonuçları da erişilemez yapabilir. Sırada bekleyen
işin token'ı çalıştırılmadan hemen önce tekrar doğrulanır; iptal edilmiş veya
süresi dolmuş token ile dosya/komut yan etkisine başlanmaz.

`get_tool_result` yanıtının `structuredContent.call` alanı durumdur;
`structuredContent.result` hedef aracın varsa yapılandırılmış çıktısıdır.
`content` içinde durum metninin ardından özgün metin/native görseller korunur.
Kaynak metadata `result_meta` alanındadır; tarihsel OAuth challenge bu alanla
taşınır, mevcut sorguya yeni bir kimlik doğrulama challenge'ı gibi uygulanmaz.
Kaynak hata sonucu `isError` ile korunur. `list_remote_tool_calls` yalnız
erişebildiğiniz kayıtları en yeni başta listeler; `next_before_id` değerini
`before_id` ile vererek eski sayfalara geçin.

Katalog en çok 256 kimlik ve dört bekleyen/çalışan çağrı tutar. Tamamlanan
kimlik/tekilleştirme anahtarı bir saat sonra sonraki katalog erişiminde silinir;
bekleyen iş unutulmaz. Katalog dolunca yeni iş reddedilir. Argümanlar en çok
32 MiB JSON'dur. Sonuç başına 8 MiB, toplam 16 MiB serileştirilmiş sonuç yükü
saklanır; Python nesne ek yükü ayrı olabilir. Toplam sınırda eski sonuç içeriği
çıkarılır, kimlik/bitmiş durumu kalır. Aşırı büyük veya serileştirilemeyen
sonuçta neden açıkça bildirilir. İçerik çıkarılması işlemi yeniden çalıştırmaz.

Kimlikler ve sonuçlar RAM'dedir; sunucu yeniden başlatılınca kaybolur. Kayıp
kimlik/sonuç, bir yazmanın veya komutun çalışmadığını **kanıtlamaz**. Yeniden
çalıştırmadan önce dosya hash'ini/işin gerçek durumunu inceleyin. Bu kimlikler
kalıcı çağrı geçmişi veya undo yerine geçmez. Kontrol araçlarının argüman ve
sonuçları kalıcı geçmişe yeniden kopyalanmaz; çalışan hedef aracın normal kapsamlı
kaydı kullanılır. İptal veya otomatik retry yoktur. Mevcut araç/helper zaman
sınırları geçerlidir; normal sunucu kapanışı kabul edilmiş işleri bitirip
sonra sahip olunan süreçleri ve geçmiş deposunu kapatır.

`write_document` varsayılan `source_format="plain"` ile `.docx` veya basit PDF
paragrafları oluşturur. DOCX için `source_format="markdown"` seçilirse
`paragraphs` listesi boş satırlarla birleştirilir; tam Markdown tek liste öğesinde
de verilebilir. `#`–`######` gerçek Heading 1–6 stillerine; kalın/eğik/kod native
run biçimine; listeler Word liste stillerine; tablolar native Table Grid'e ve
alıntılar Quote stiline dönüşür. Kod blokları tek aralıklı yazı kullanır.
Bağlantı adresleri görünen metin olarak saklanır, dış ilişki oluşturulmaz.
Görseller ve desteklenmeyen HTML öğeleri reddedilir; ağdan içerik alınmaz.
Bu temel Markdown alt kümesidir: özel CSS, tam HTML düzeni ve Word'ün bütün
özellikleri uygulanmaz. Birleştirilmiş metin karakter bütçesine ve 64 seviye
işaretleme iç içelik sınırına uyar; çıktı mevcut dosya/undo bütçesine tabidir.
Yazma izni, kaynak SHA, atomik kayıt ve geri alma değişmez. Görsel düzen sadakati
bu yapısal DOCX testlerinin kanıtı değildir; zengin PDF için `write_pdf` kullanın.

`read_multiple_files` tek çağrıda 1..16 metin/belge penceresi ile PNG/JPEG/GIF/WebP
görselini birlikte döndürür. İlk metin içerik bloğundaki `files` listesi girdi
sırasını korur; aynı liste MCP `structuredContent` alanında da bulunur. Görsel
kaydının `content_index` değeri yanıttaki native görsel bloğunu gösterir; SHA,
özgün bayt sayısı, MIME ve boyutları eşlik eder. Tekli/çoklu görsel okuması aynı
format, bütünlük ve Pillow piksel sınırlarını kullanır. Görsel dosyaları toplam
4 MiB özgün bayt bütçesini paylaşır (base64/protokol ek yükü ayrıca oluşur).
Metin JSON'u mevcut `max_output_chars` bütçesine uyar; JSON kaçışları ve dosya
etiketleri hesaba katılır. Gerekirse metin penceresi küçültülür ve kayıpsız
devam için `next_offset` döner. Sığmayan kayıt `response_budget_exceeded` hatası
olur; bir dosyanın hatası diğer sonuçları kaldırmaz. Uzantısı görsel olmayan
dosyalar belge/metin hattından geçer; özgün görsel okumak için `read_image` da
kullanılabilir. Devam penceresi ilgili dosyanın `read_file` çağrısıyla alınır.

`read_process_output` içindeki negatif `offset` çıktı sonuna göre bayt konumudur:
`offset=-1000` son 1000 bayttan başlar ve `length` kadarını döndürür. Pozitif
offset ve dönen `next_offset` mutlak bayt konumudur; sonraki okumayı bu imleçle
sürdürün. `available_start`/`available_end` RAM halka tamponunda kalan aralığı,
`requested_data_dropped` istenen başlangıcın atılmış olup olmadığını belirtir.
Kaybolmuş bölüm istenirse okuma kalan ilk bayttan başlar; tamamı okundu diye
sunulmaz. UTF-8 karakterinin ortasından başlayan/biten bayt penceresinde yerine
koyma karakteri görülebilir; varsayılan bayt modu satır sayısı veya terminal emülasyonu değildir.

Aynı araçta `unit="lines"` seçilirse offset/length LF ile ayrılmış satırlardır;
CRLF baytları korunur. Varsayılan uzunluk baytta 16000, satırda 1000'dir; üst
sınırlar 65536 bayt ve 2000 satırdır. Satır yanıtında `available_start/end`
global satır aralığıdır; `available_byte_start/end` ve `next_byte_offset` ayrıca
verilir. Tampon ortadan kesilmişse `first_line_prefix_dropped` doğrudur.
Canlı çıktının son satırı tamamlanmamışsa `last_line_incomplete=true` olur;
`next_offset` o satırı yeniden gösterecek şekilde kalır. Kayıpsız canlı devam için
`next_byte_offset` ile `unit="bytes"` kullanın. Tek satır karakter bütçesini
aşarsa satır modunda hata verilir; bayt pencerelerine geçin.

Bayt modunda açık `offset=null`, sunucunun oturum başına yeni çıktı imlecini
tüketir; mutlak/tail okumaları bu imleci değiştirmez. İmleç tüm istemcilerin
aynı süreç oturumunda paylaştığı RAM durumudur. `wait_ms=0..30000`, istenen
konumda veri yoksa çıktı/EOF/süreç bitişi bildirimini bekler; sıfır beklemeden
okur. `wait_status` veri, bitiş, çıktı kapanışı, zaman aşımı veya beklemesiz
okumayı; `waited_ms` gerçek bekleme/okuma süresini bildirir. Sürekli timer/poll
döngüsü kullanılmaz. MCP araçları sıralı yürüdüğü için uzun bekleme başka
araç çağrısını da geciktirir; REPL için genellikle kısa bekleme seçin.

REPL'de ilk çıktı baytından sonra istemin tamamlanmasını beklemek için
`read_process_output(..., wait_ms=3000, wait_for_prompt=true)` kullanın.
Bu seçenek yeni/okunmamış çıktı ile tanınan istemi, süreç bitişini veya EOF'yi
bekler. `wait_status=prompt_detected` tanınan istemi, `timeout` ise bekleme
bütçesinin dolduğunu gösterir; timeout yanıtı kısmi çıktı içerebilir.
İstem baytları çıktıdan silinmez. `offset=null` ile tüketilmiş eski istem
yeniden yeni çıktı sayılmaz. Stdin kapanması istendikten sonra istem ipucu
üretilmez. Tanıma hâlâ sezgiseldir; gerçek OS giriş beklemesi veya PTY değildir.

`stdin_closed`, `stdin_close_requested`, `stdin_write_in_progress`,
`stdin_error` ve giriş kuyruğu boyut/kapasitesi gözlenen pipe durumudur.
Kapanan/kapanması istenmiş stdin'e yeni giriş reddedilir. `prompt_hint` yalnız
Python `>>> `/`... ` veya genel `> ` gibi tam son satır için sezgisel ipucudur;
renkli/özel istemler bulunmayabilir ve normal çıktı da isteme benzeyebilir.
`waiting_for_input=null` işletim sistemi seviyesinde giriş beklemenin bilinmediğini
gösterir; ipucu gerçek engellenme kanıtı gibi sunulmaz. PTY veya ANSI ekran
emülasyonu eklenmemiştir.

Windows'ta her oturumun küçük bir Python başlatıcısı vardır. Başlatıcı `-I -S`
ile açılır ve komut argv'sini yalnız Job Object bağlandıktan sonra uzunluklu
ikili stdin çerçevesinden alır; çok kısa komutun sahiplik kurulmadan çalışıp
bitmesi/çocuk oluşturması engellenir. Çerçeve tam uzunlukta ve tamponlamasız
okunur; komutun stdin verisi değişmez. Ek başlatıcı süreç belleği bu güvence
için maliyettir; ayrı sürekli polling yapmaz. Dönen `pid_role=owned_launcher`
PID'nin komutun kendisi değil oturum başlatıcısı olduğunu gösterir; Unix'te
`pid_role=command` kalır. Komutun çıkış kodu aktarılır. Windows'ta komutun
başlatılamaması oturum çıktısında hata sınıfı ve 127 (bulunamadı) veya 126
(diğer başlatma/çerçeve hatası) ile görülür; `start_process` yanıtı komutun
başarıyla tamamlandığı anlamına gelmez. Bu sahiplik mekanizması işletim sistemi
kullanıcısının komut yetkisini sandbox'a dönüştürmez.

`get_processes` sayfalı listeye ek olarak `pid` ile tek süreç gözlemi sunar.
Komut satırı/ortam değişkenleri döndürülmez. `cpu_user_seconds`,
`cpu_system_seconds` ve toplam `cpu_seconds` birikimli sayaçlardır.
`cpu_percent`, yüksek çözünürlüklü monoton saatle iki gözlem arasından hesaplanır;
`cpu_interval_seconds` ölçüm aralığını verir. İlk, süresi dolmuş, kimliği değişmiş
veya sayacı gerilemiş gözlemde yüzde `null` olur. %100 bir mantıksal CPU'dur;
çok çekirdekli kullanım %100'ü aşabilir. Çok kısa aralıklar gürültülüdür;
[psutil belgeleri](https://psutil.readthedocs.io/stable/#psutil.Process.cpu_percent)
doğruluk için en az 0,1 saniye aralık önerir. Sunucu kendisi uyumaz veya
arka planda CPU örnekleyicisi açmaz. En çok 4096 kimlikli örnek, 60 saniye boşta
kalma ile RAM'de tutulur; temizlik gözlem sırasında yapılır. Sayfa karakter
bütçesine göre erken bitebilir; değişen PID listesinde offset bir snapshot değildir.

`kill_process` için aynı gözlemden alınan `pid` ve tam `process_key` gerekir.
Bu kimlik bir parola/izin değildir; PID yeniden kullanımı çatışmasını denetler.
Yerel `--allow-exec`, OAuth'ta ayrıca `folderlens:exec` ve mevcut OS izinleri
gerekir; yalnız okuma/yazma OAuth yetkisi yeterli değildir. Windows'ta oluşturulma
FILETIME değeri ve sonlandırma aynı açık process handle'ı üzerinden denetlenir
([GetProcessTimes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes),
[TerminateProcess](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-terminateprocess)).
Linux'te `pidfd_open`/`pidfd_send_signal` gerekir; başka platformda güvensiz PID
sinyaline otomatik fallback yapılmaz. Linux backend'i bu Windows oturumunda
çalıştırılarak doğrulanmamıştır. FolderLens'in kendisi, ataları, init ve kritik
Windows süreçleri korunur; yetki yükseltilmez. Aktif bir FolderLens oturum kökü
seçilirse sahipli ağaç temizliği kullanılır; diğer durumda yalnız seçilen süreç
zorla sonlandırılır ve kaydedilmemiş çalışması kaybolabilir. `wait_ms=0..5000`
sonrası `terminated` kontrol edilmelidir; isteğin kabulü bitiş kanıtı değildir.
Süreç adı/RSS erişilemiyorsa alanlar `null` veya kayıt `unavailable` olur;
eksik süreç kimliğiyle sonlandırma yapılmaz.

`get_file_info` metinde kodlama, `line_count` ve sıfır tabanlı `last_line` döndürür.
Oluşturulma zamanı platformca biliniyorsa `created_ns` vardır; Unix ctime değeri
oluşturulma zamanı gibi sunulmaz. Mode bitleri Windows ACL'sinin karşılığı değildir.

`read_pdf` sıfır tabanlı `page_index` ile tek sayfa okur. `mode="text"` için
`offset`/`length` karakter penceresidir; `next_offset` ile aynı sayfaya devam,
`next_page_index` ile sonraki sayfaya geçiş yapılır. `mode="images"` ve boş
`image_index`, sayfadaki gömülü görsel anahtarlarını sayfalar: `offset` katalog
indeksidir, `length` katalog JSON karakter bütçesidir. `image_index` verilirse
seçilen görsel metadata ve native MCP içerik olarak döner; `content_index=1`
görsel bloğudur. `expected_sha256` değişen PDF'ye karşı devam kontrolüdür.
Görsel çıkarımı PDF akışını çözebilir/yeniden paketleyebilir; fiziksel PDF stream
baytlarının kopyası olduğu iddia edilmez. JPEG/PNG/GIF/WebP dışındaki çıkarımlar
PNG'ye dönüştürülür ve `converted_to_png` belirtilir; `image_sha256` dönen
görselin hash'idir. Sayfa önizlemesi için ayrı `render_pdf_page` kullanılır.
Okuma yardımcı süreci 512 MiB süreç bellek sınırı, 45 saniye toplam süre,
16 MiB PDF, 2000 sayfa ve seçilen görselde 4 megapiksel/4 MiB sınırı uygular.
Şifreli PDF reddedilir; OCR, PDF JavaScript'i/makro/aksiyon çalıştırma veya
görsel düzen sadakati sunulmaz. Metin çıkarımı motorun mantıksal okumasıdır.

`list_directory` için `depth=1` yalnız doğrudan içerik (uyumlu varsayılan),
`depth=2` bir alt seviye, daha büyük değerler daha derin görünüm sağlar.
Yollar çalışma köküne göredir; her girdide `depth` bulunur. Genişlik öncelikli
dosya sistemi sırasında tüm seviyeler birlikte `limit=1..500` ve yanıt karakter
bütçesiyle sayfalanır; `next_offset` değerini aynı `path`/`depth` ile kullanın.
Tarama anlık görüntü değildir: dizin değişiklikleri sayfaları değiştirebilir.
Gizli/üretilmiş/korunmuş yollar ve bağlantılar listelenmez veya takip edilmez.
Erişilemeyen alt dizin `kind=error, error=directory_unavailable` girdisiyle
bildirilir; kök erişim hatası çağrıyı başarısız yapar. `status=page_limit`
devam sayfasını, `completed` istenen derinliğin bitişini gösterir. Her çağrı
`max_scan_files` ve iki saniyelik keşif dilimiyle sınırlıdır; `scan_limit` veya
`time_limit` eksik sonucu belirtir ve daha dar bir alt dizin seçilmesini gerektirir.
Tarama süresi kontrolü dosya sistemi çağrıları arasındadır; bloke olmuş işletim
sistemi çağrısı için kesin bir duvar saati iptal garantisi değildir.

MCP `folderlens://guide` kaynağı ve `agent_workflow` prompt'u da vardır. Başlangıç
talimatları incele → planla → uygula → doğrula → raporla akışını açıklar.

`write_file` varsayılan `mode="rewrite"` ile UTF-8 tam içerik değiştirir.
`mode="append"` yalnız verilen metni dosyanın sonuna ekler; kendiliğinden satır
sonu eklemez. Mevcut UTF-8, BOM'lu UTF-8/UTF-16/UTF-32 dosyalarında özgün baytlar,
BOM ve byte sırası korunur. Belge biçimleri, NUL içeren veya kodlaması belirsiz
dosyalar append modunda reddedilir. Parçalı yazımda ilk parçayı rewrite, sonraki
parçaları append ile gönderin; her çağrıda bir önceki başarılı kaydın `sha256`
değerini kullanın. Aynı eski hash ile tekrar gönderim çatışma üretir. `null` yalnız
yeni dosyaya izin verir. Her parça ayrı atomik kayıt ve ayrı undo'dur; bütün seri
tek işlem değildir ve toplam dosya/undo bütçeleri geçerlidir. `preview_changes`
aynı `mode` parametresiyle yazma yetkisi olmadan diff ve sonuç hash'ini gösterir.

Örnek sohbet isteği:

> FolderLens get_config ile kapsamı kontrol et. AGENTS.md ve README'yi oku, Git
> geçmişini incele. İstediğim değişikliği önce önizle, sonra uygula ve ilgili
> testleri çalıştır. Gerçekte çalıştırdığın testleri ve kalan eksikleri bildir.

## Sınırlar ve referans karşılaştırması

Özellik sınırları ve doğrulama kapsamı [TR/EN kurulum rehberinde](https://yavuzselimtech.tr/folderlens/agent/?lang=tr#limits)
izlenir. Araç sayısı, bu tablodaki açıkların kapandığı anlamına gelmez.

Remote Desktop Commander araç tanımları dosya/dizin işlemleri, belge okuma,
metin/Office düzenleme, arama oturumları, süreç/REPL yönetimi, yapılandırma,
istatistik ve uzak cihaz yönetimi içeriyordu. İnceleme tarihinde bağlı cihaz
çevrimdışıydı; bu yüzden aynı cihazda performans veya uçtan uca ürün karşılaştırması
yapılmadı. “Tüm özelliklerinden daha iyi” ölçülmüş bir sonuç değildir.

Bu çekirdekte eklenen belirgin yetenekler: orijinal dosya SHA-256'sıyla çatışma
kontrolü, varsayılan diff önizlemesi, kontrollü geri alma, salt okunur Git araçları,
belge araçlarıyla aynı yerel yol politikası, varsayılan içeriksiz RAM kaydı,
isteğe bağlı kapsam denetimli kalıcı geçmiş ve
paylaşım sunucusundan ayrılmış MCP taşımasıdır.

- Sunucu başına tek işlem kökü sunulur; ek cihazlar sahip tarafından tanımlanan
  katalogda listelenir ve normal araçlar `call_device` ile yönlendirilir. Bulut cihaz hesabı/uzak agent kurulum
  hizmeti yoktur. Konfigürasyon sohbetten genişletilemez; yerel yeniden başlatma gerekir.
- Dosyalar en çok 16 MiB, normal yanıt metin penceresi en çok 64 Ki karakterdir.
  Sayfalama karakter tabanlıdır; JSON kaçışları ve metadata tel boyutunu artırabilir.
  `read_lines` düz metinde sıfır tabanlı satır offset'i veya sondan negatif offset
  kabul eder: `offset: -20` son 20 satırın başından okur. `line_count` 1..2000;
  yanıt karakter bütçesinde daha erken kesilebilir. `next_offset` ile devam edin,
  `expected_sha256` göndererek değişen dosyayı reddedin. Satır sonları korunur.
  Tek satır bütçeden büyükse karakter pencereli `read_file` kullanılmalıdır.
- Arama en çok 8 oturum, 5 dakika boşta kalma, 100.000 dosya ve dosya başına 1 MiB
  sınırına sahiptir. `pattern_mode` varsayılan `literal`, isteğe bağlı `regex` veya
  yalnız dosya yolu aramasında `glob` olabilir. `file_pattern` ek glob filtresidir.
  Regex satır bazında çalışır; eşleştirme başına 50 ms motor içi timeout uygulanır.
  `regex_timeout`, `scan_limit` ve atlanan dosyalar eksik kapsamı bildirir.
  `context_lines` 0..10 önceki/sonraki satırı; `max_results` 1..100000 sonuçta
  `result_limit` ile erken bitirmeyi seçer. Sayfa en çok 100 sonuç/2 saniye/1000
  yeni dosya tarar; `has_more` bitene kadar sürdürün. Tek dosyadaki eşleşmeler
  artık 100'de kesilmez; mevcut dosyanın en çok 1 MiB metni devam sayfaları için
  RAM'de tutulur ve oturum bitişinde bırakılır. Sonuçlar kaynak SHA-256, satır,
  en çok 500 karakterlik metin/bağlam ve kırpılma bilgisini taşır. Dosya taranırken
  alınan görünümü temsil eder; bütün çalışma ağacının atomik anlık görüntüsü değildir.
  Tek sonuç yanıt bütçesini aşarsa daha az bağlamla yeniden arama istenir.
  `get_more_search_results` içinde `offset` verilmezse tarama devam eder. Açık
  `offset` sıfır tabanlı, daha önce teslim edilmiş sonuç indeksidir; negatif
  değer `history_end` sonuna göredir (`-5`: teslim edilmiş son beş sonuç).
  Geçmiş okuması dosyayı yeniden okumaz ve tarama imlecini ilerletmez; kaynak
  SHA ve ilk bulunan metin korunur. Her oturum en çok 1 MiB UTF-8 JSON sonuç
  geçmişini RAM'de tutar; Python nesne/metadata ek yükü bu serileştirilmiş bütçeden
  ayrıdır. `history_start`/`history_end` kalan yarı açık aralığı,
  `requested_results_dropped` istenen eski sonuçların atıldığını gösterir.
  Atılmış indeks istenirse kalan ilk sonuçtan başlanır. Geçmişte `has_more`
  yalnız daha fazla kayıtlı sonucu, `search_has_more` ise taramanın devam
  edebileceğini belirtir; devam için `offset` alanını kaldırın. `next_offset`
  her zaman mutlak sonuç indeksidir. Tamamlama geçmişi hemen silmez; `stop_search`,
  beş dakika hareketsizlik veya sunucu kapanışı geçmişi bırakır. `list_searches`
  kalan aralık/bayt bütçesini de gösterir. Bütçe nedeniyle geçmişin kısalması
  normal tarama sayfalarında sonuç atlamasına yol açmaz.
- Terminal en çok 8 oturum, oturum başına 1 MiB çıktı halkası, en çok bir saat
  timeout ve tamamlanmış oturumlarda bir saatlik saklama süresi kullanır.
  `dropped_bytes`, `next_offset`, `exit_code` ve `timed_out` kontrol edilmelidir.
  Pipe REPL desteklenir; PTY/fullscreen terminal ve GUI bilgisayar kontrolü yoktur.
  Windows Job Object, Unix süreç grubu ve kapanış temizliği çocuk süreçleri sınırlar;
  komut çalıştırma güvenlik sandbox'ı sağlamaz.
- Word/Excel/PDF/PPTX/EPUB/notebook okumaları mevcut FolderLens çıkarıcısını kullanır.
  OCR ve görsel düzen sadakati vaat etmez. Excel formülleri hesaplanmaz. Yeni belge
  yazımı basit paragraflarla sınırlıdır. `edit_word`, aynı paragraftaki stilli metin
  parçalarını, tabloları, üstbilgi/altbilgi ve dipnot XML parçalarını tarar; yeni
  metin ilk eşleşen parçanın biçimini alır. Tab, satır sonu ve alan sınırlarının
  üzerinden eşleşme kurmaz. Önizleme varsayılandır; beklenen toplam eşleşme sayısı
  tutmazsa hiçbir dosya yazılmaz. `edit_pdf`, sıfır tabanlı sayfa indeksleriyle
  kaynak PDF'den ekleme, silme veya 90 derece katlarıyla döndürme yapar; tüm
  sayfaların silinmesini ve şifreli PDF'leri reddeder. Bu düzenlemeler de aynı
  SHA-256/undo mekanizmasını kullanır.
- `read_word` Word gövdesini `outline` modunda blok indeksi, stil, tablo metni ve
  görsel ilişki kimlikleriyle sayfalar. Blok metni 2000 karakterde, görsel listesi
  20 referansta kırpılır; kırpılma bildirilir. `parts` modu XML parçalarını sayfalar;
  `xml` modu seçili parçanın pretty-printed XML'ini sıfır tabanlı satırlarla okur.
  Negatif satır offset'i tail, `xml_chars` ise uzun satırlarda karakter penceresi
  sağlar. Her modda `next_offset` ve devam okumasında `expected_sha256` kullanın.
  `edit_word_xml`, bu görünümden kopyalanan tam XML parçasını, belirtilen
  `word/*.xml` dosyasında değiştirir. Varsayılan önizlemede bounded diff döner;
  `preview: false` aynı hash/undo hattında uygular. XML kökü ve ana belgenin tek
  gövdesi korunmalıdır. DOCTYPE ve bozuk XML reddedilir; diğer ZIP üyeleri korunur.
  Bu doğrulama XML/paket yapısını denetler; tam OOXML şemasını veya Word'deki
  görsel yerleşimi onayladığı anlamına gelmez. Stil değişimini Word'de ayrıca açıp
  kontrol edin. Normal metin değişikliği için run biçimini koruyan `edit_word`
  daha doğrudandır.
- Excel araçları `.xls`, `.xlsx` ve `.xlsm` okuyabilir. `list_workbook_sheets`
  ad/indeks listesini `offset` ve `limit` (1..500) ile sayfalar. `read_workbook`
  sayfa adı veya sıfır tabanlı indeks ve en çok 10.000 hücrelik dikdörtgen aralık
  kabul eder. Sayısal bir string gerçek sayfa adıyla eşleşirse ad olarak kullanılır;
  aksi halde indeks sayılır. Sayfalarda `expected_sha256` ile değişen dosyayı reddedin.
  Modern dosyalarda formül ifadeleri hesaplanmadan döner. Eski `.xls` dosyalarında
  yalnız dosyaya kaydedilmiş formül sonuçları okunabilir; bu durum
  `formula_representation: cached_values_only` ile belirtilir. Tarihler ISO metni,
  boolean/hata/boş hücreler uygun türle döner. `.xls` satır/sütun sınırları ve
  `.xlsx/.xlsm` sınırları ayrı denetlenir.
- `write_workbook` yeni `.xlsx`/`.xlsm` oluşturur; mevcut dosyaları
  düzenler. Yeni `.xlsm` makro etkin bir kapsayıcıdır; VBA projesi içermez.
  `null` hücreyi temizler. Makrolar ve formüller çalıştırılmaz; `.xlsm`
  kaydında VBA koruması, tüm modern güncellemelerde dış bağlantı koruması açılır.
  Kaybolan ZIP parçaları (paylaşılan string/formül hesaplama önbelleği hariç),
  değişen binary parçaları, desteklenmeyen özellik uyarıları ve dijital imzalı
  dosyalar yazmadan reddedilir. Bu kontroller tam Excel özellik/görsel sadakati
  garantisi değildir; kritik dosyaları Excel'de ayrıca doğrulayın.
  VBA testi sentetik binary parçanın birebir korunmasını kanıtlar; makro yürütme
  veya gerçek VBA projesinin davranış testi değildir.
- Windows'ta gerçek BIFF8 `.xls` oluşturma/düzenleme için sahibi terminalde
  `python folderlens.py --setup-xls` çalıştırır. Kurulum, sürüm ve SHA kilitli
  Apache POI 6.0.0/JAR bağımlılıklarını özel `.folderlens/xls-runtime` dizinine
  indirir; Java 17+ yoksa Windows x64 için Zulu JRE 21 sağlar. Başka mimarilerde
  sahibi Java 17+ kurmalıdır (`FOLDERLENS_JAVA` tam çalıştırılabilir yolu).
  Derleyici yalnız sabit `NativeXls.java` kaynağını derler; araç çağrısı kurulum,
  indirme veya kullanıcı kodu derleme yapmaz. JAR lisansları kendi META-INF
  dizinlerinde, JRE lisansları dağıtımın `legal` dizinindedir. Kilit tarifi
  `agentarm/legacy_excel.lock.json` dosyasındadır.
  Yazım 30 saniyelik Java alt sürecinde, 128 MiB Java heap ve Windows'un
  512 MiB süreç bellek sınırıyla yapılır; ana helper'ın toplam 45 saniye sınırı
  geçerlidir. Excel açılmaz; formüller/makrolar çalıştırılmaz. Birleştirilmiş
  hücrede yalnız sol üst hücre değişebilir. Mevcut OLE düğümleri korunarak
  yazılır; tüm eski Excel özelliklerinin sadakati garanti edilmez. SHA/atomik
  kayıt/undo kontrolleri değişmez. `.xls` yazımı diğer işletim sistemlerinde
  henüz etkin değildir; okuma platformlar arasıdır.
- Excel ayrıştırma/kayıt işlemleri sabit amaçlı ayrı Python sürecinde yapılır;
  genel komut izni açılmaz ve Excel uygulaması başlatılmaz. Girdi 32 MiB JSON,
  kaynak/çıktı dosyası etkin dosya bütçesi, süre 45 saniye ile sınırlıdır.
  Windows'ta süreç commit belleği, Unix'te sanal adres alanı 512 MiB ile sınırlanır.
  Hata/timeout'ta kaynak yazılmaz ve helper temizlenir. PDF ve Excel helper'ları
  aynı yönetilen süreç/JSON/çıktı/geçici dizin yaşam döngüsünü paylaşır; PDF'nin
  tarayıcı renderer'ına Excel bellek sınırı uygulanmaz.
- Zengin PDF için `write_pdf` başlık, liste, tablo, kod bloğu, HTML/CSS, satır içi
  SVG ve göreli yerel görselleri destekler. Sayfa sonu için
  `<div style="page-break-before: always;"></div>` kullanılabilir. A4/Letter,
  yatay/dikey seçenekleri vardır. Playwright Chromium yoksa Windows'ta Edge
  kullanılır; gerekirse `.venv` Python'uyla `python -m playwright install chromium`
  çalıştırın. Bağımsız helper sürecinin sayfa ağı ve JavaScript'i kapalıdır;
  CSP, script/iframe temizliği ve kök içi görsel denetimi uygulanır. İçerik+CSS
  64 Ki karakter, görsel başına 4 MiB/16 megapiksel, toplam görsel ve PDF 16 MiB,
  sonuç 2000 sayfa ve helper süresi 45 saniye ile sınırlıdır. Bu sabit amaçlı
  helper genel komut yetkisini açmaz. Profil/geçici dosyalar başarıda ve timeout'ta
  temizlenir. `render_pdf_page` ayrı PDFium sürecinde en çok 4 megapiksel JPEG
  önizleme üretir; kaynak dosya hash'ini ve sayfa kimliğini de döndürür.
- URL okuma yalnızca başlangıçta `--allow-url-host example.com` ile belirtilen tam
  DNS adlarında açılır; seçenek birden çok kez verilebilir. HTTPS/443 kullanır,
  TLS sertifikasını doğrular, her yönlendirme ve bütün DNS adreslerini denetler,
  doğrulanmış IP'ye bağlanırken orijinal TLS hostname'ini korur. Özel ağ/loopback/
  metadata/multicast adreslerini, URL içi kimlik bilgilerini, sıkıştırılmış gövdeleri
  ve 1 MiB üzeri yanıtları reddeder. Ortam proxy'lerini ve çerezleri kullanmaz.
  Metin, JSON/XML ve PDF okunur; HTML kaynak olarak döner, çalıştırılmaz.
  Ağ okumasında 30 saniyelik toplam süre bütçesi ve 10 saniyelik socket timeout
  uygulanır; işletim sisteminin DNS çözümlemesi bu timeout'tan ayrı çalışır.
  En çok dört yanıt 60 saniye RAM'de tutulur; sayfalarda `expected_sha256`
  göndererek önbellek süresi sonunda değişen içeriği tespit edin. `--allow-exec`
  açıkken bu URL filtresi keyfi komutların ağ erişimini sınırlandırmaz.
- Hizmet farkları arasında üreticinin bulut cihaz hesabı ve uzak agent kurulum/bağlantı
  hizmetidir. FolderLens, yerel veya sahibi tarafından güvenli erişime açılmış
  bağımsız MCP sunucularını kullanır; üçüncü taraf hesabını veya faturalama sistemini
  kopyalamaz. Referansla hesap/servis düzeyinde tam eşitlik ilan edilmez.

## Doğrulama

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'
.\.venv\Scripts\python.exe -m ruff check agentarm run.py folderlens.py tests
.\.venv\Scripts\python.exe -m ruff format --check agentarm run.py folderlens.py tests
npm test
```

Testler sentetik geçici klasörlerde gerçek STDIO ve TCP/HTTP MCP istemcileri, HTTP initialize/auth ve
Host/Origin denetimi, dosya hash/undo, kodlama, symlink/junction, arama, Excel
formülleri/boş hücreler, Unicode Word/PDF ve süreç timeout/kapanışını kapsar.
STDIO komut çıktısı/çıkış kodu ile HTTP üzerinden dosya yazma/okuma, yetki iptali,
araç hatasının MCP hata sonucuna dönüşümü ve temiz sunucu kapanışı ayrıca denetlenir.
OAuth testleri gerçek sertifikası doğrulanan yerel HTTPS üzerinden resmî SDK ile
keşif, DCR, PKCE, okuma → yazma → komut izni yükseltme ve süreç çıktısını kapsar.
Token rotasyonu/iptali, CSRF, callback/Host/Origin, yanlış kökte token reddi ve
özel SQLite dizini de geçici sentetik verilerle sınanır.
ChatGPT'nin kendi hesabınızda araç seçimini yapabildiğini son bağlantıdan sonra
`get_config` çağrısıyla ayrıca denetleyin; SDK istemci testi hesap/UI testi değildir.

## Kaynaklar

- [OpenAI: MCP ve masaüstü/CLI/web bağlantı yolları](https://learn.chatgpt.com/docs/extend/mcp)
- [OpenAI: uzak MCP araçları ve güvenlik](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)
- [OpenAI: MCP plugin bağlantısı](https://developers.openai.com/plugins/deploy/connect-chatgpt)
- [OpenAI: Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
- [Resmî MCP Python SDK](https://py.sdk.modelcontextprotocol.io/)
- [lxml: güvenli XML ayrıştırma seçenekleri](https://lxml.de/parsing.html)
- [xlrd: eski Excel biçimlerinin okuma API'si](https://xlrd.readthedocs.io/en/latest/api.html)
- [openpyxl: çalışma kitabı yükleme ve VBA koruma sınırları](https://openpyxl.readthedocs.io/en/stable/tutorial.html)
- [Python: platformlara göre rename davranışı](https://docs.python.org/3/library/os.html#os.rename)
- [Linux: renameat2 ve RENAME_NOREPLACE](https://man7.org/linux/man-pages/man2/rename.2.html)

### Sahibin seçtiği PDF tarayıcısı

FOLDERLENS_PDF_BROWSER_CHANNEL ortam ayarı yalnız chrome veya msedge kabul eder.
Kurulu tarayıcıyı seçmek, Linux/AppArmor'da indirilen Chromium için engellenen
sandbox başlangıcına alternatif sağlar; sandbox, çevrimdışı ağ ve JavaScript
yasağı korunur. Ayarı istemcinin MCP env bölümüne ekleyip ajanı yeniden başlatın.
Varsayılan yönetilen Chromium ve Windows Edge yedeği değişmez. Keyfi yürütülebilir
yol/bayrak kabul edilmez; normal tarayıcı profili kullanılmaz.


### Süreç zamanlaması

`read_process_output(..., verbose_timing=true)` monotonik saatle toplam gözlem
süresini, ilk çıktı gecikmesini ve son 200 pipe okuma olayını döndürür.
Tamamlanma süresi süreç ağacı/okuyucu temizliği sonundaki gözlemdir; tamamlandıktan
sonra sabit kalır. Çıktı yoksa first_output_ms null olur. Her olay mutlak byte
aralığı ve sırasını taşır; dropped_events eski olay kaybını bildirir. Olaylar
çocuk programın write çağrılarıyla birebir değildir; OS pipe okumaları birleşebilir.
Metin ikinci kez tutulmaz. Varsayılan yanıtta timing alanı yoktur.

İlk çıktıyı ayrı bir çağrı yapmadan almak için `start_process` üzerinde de
`wait_ms`, `wait_for_prompt` ve `verbose_timing` kullanılabilir. Örneğin Python
REPL için `wait_ms=3000, wait_for_prompt=true` tanınan isteme kadar bekler.
Bu bekleme süresi süreç ömrünü belirleyen `timeout_seconds` değerinden ayrıdır;
süre dolduğunda çalışan iş arka planda devam eder. Dönen `next_offset` ile
`read_process_output` çağrısına devam edin. Varsayılan `wait_ms=0` ve kapalı
bayraklar önceki hızlı başlatma yanıtını korur.
