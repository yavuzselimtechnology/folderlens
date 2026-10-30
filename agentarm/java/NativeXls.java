import java.io.ByteArrayInputStream;
import java.io.DataInputStream;
import java.io.FilterOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import org.apache.poi.hssf.usermodel.HSSFWorkbook;
import org.apache.poi.ss.usermodel.Cell;
import org.apache.poi.ss.usermodel.Row;
import org.apache.poi.ss.usermodel.Sheet;
import org.apache.poi.ss.util.CellRangeAddress;

/** Fixed, non-evaluating BIFF8 cell writer. Inputs are data, never Java source. */
public final class NativeXls {
    private static final int MAX_FILE_BYTES = 16 * 1024 * 1024;
    private static final int MAX_CELLS = 10000;
    private static final int XLS_ROWS = 65536;
    private static final int XLS_COLUMNS = 256;

    private static byte[] readBytes(DataInputStream input, int length, int maximum) throws IOException {
        if (length < 0 || length > maximum) {
            throw new IOException("Input field exceeds its byte budget");
        }
        byte[] bytes = new byte[length];
        input.readFully(bytes);
        return bytes;
    }

    private static String readString(DataInputStream input) throws IOException {
        return new String(readBytes(input, input.readInt(), 4 * 64 * 1024), StandardCharsets.UTF_8);
    }

    private static final class LimitedOutput extends FilterOutputStream {
        private final int maximum;
        private int written;

        LimitedOutput(OutputStream target, int maximum) {
            super(target);
            this.maximum = maximum;
        }

        @Override
        public void write(int value) throws IOException {
            if (++written > maximum) {
                throw new IOException("Generated XLS exceeds its file budget");
            }
            out.write(value);
        }

        @Override
        public void write(byte[] data, int offset, int length) throws IOException {
            if (length > maximum - written) {
                throw new IOException("Generated XLS exceeds its file budget");
            }
            written += length;
            out.write(data, offset, length);
        }
    }

    private static void writeWorkbook() throws IOException {
        DataInputStream input = new DataInputStream(System.in);
        byte[] magic = readBytes(input, 6, 6);
        if (!Arrays.equals(magic, "FLXLS1".getBytes(StandardCharsets.US_ASCII))) {
            throw new IOException("Unsupported XLS helper protocol");
        }
        int budget = input.readInt();
        if (budget < 1 || budget > MAX_FILE_BYTES) {
            throw new IOException("Invalid XLS file budget");
        }
        int sourceLength = input.readInt();
        byte[] source = sourceLength == -1 ? null : readBytes(input, sourceLength, budget);
        String name = readString(input);
        int startRow = input.readInt();
        int startColumn = input.readInt();
        int rowCount = input.readInt();
        if (startRow < 0 || startColumn < 0 || rowCount < 1 || rowCount > MAX_CELLS
                || startRow + rowCount > XLS_ROWS) {
            throw new IOException("Invalid XLS row window");
        }
        try (HSSFWorkbook book = source == null ? new HSSFWorkbook()
                : new HSSFWorkbook(new ByteArrayInputStream(source), true)) {
            Sheet sheet = book.getSheet(name);
            if (sheet == null) {
                sheet = book.createSheet(name);
            }
            int cells = 0;
            for (int rowIndex = 0; rowIndex < rowCount; rowIndex++) {
                int columnCount = input.readInt();
                cells += columnCount;
                if (columnCount < 0 || cells > MAX_CELLS || startColumn + columnCount > XLS_COLUMNS) {
                    throw new IOException("Invalid XLS cell window");
                }
                int rowNumber = startRow + rowIndex;
                Row row = sheet.getRow(rowNumber);
                if (row == null) {
                    row = sheet.createRow(rowNumber);
                }
                for (int column = 0; column < columnCount; column++) {
                    int columnNumber = startColumn + column;
                    for (CellRangeAddress merged : sheet.getMergedRegions()) {
                        if (merged.isInRange(rowNumber, columnNumber)
                                && (rowNumber != merged.getFirstRow() || columnNumber != merged.getFirstColumn())) {
                            throw new IOException("Cannot write a non-anchor merged cell");
                        }
                    }
                    Cell cell = row.getCell(columnNumber);
                    if (cell == null) {
                        cell = row.createCell(columnNumber);
                    }
                    // POI otherwise treats a value assignment to a formula as a cached result.
                    // Clearing the value/type retains the existing cell style.
                    cell.setBlank();
                    int kind = input.readUnsignedByte();
                    if (kind == 1) {
                        String text = readString(input);
                        if (text.startsWith("=")) {
                            cell.setCellFormula(text.substring(1));
                        } else {
                            cell.setCellValue(text);
                        }
                    } else if (kind == 2) {
                        double value = input.readDouble();
                        if (!Double.isFinite(value)) {
                            throw new IOException("Excel numbers must be finite");
                        }
                        cell.setCellValue(value);
                    } else if (kind == 3) {
                        cell.setCellValue(input.readBoolean());
                    } else if (kind != 0) {
                        throw new IOException("Unsupported XLS cell type");
                    }
                }
            }
            if (cells == 0 || input.read() != -1) {
                throw new IOException("Empty or trailing XLS helper input");
            }
            book.setForceFormulaRecalculation(true);
            book.write(new LimitedOutput(System.out, budget));
            System.out.flush();
        }
    }

    public static void main(String[] arguments) {
        try {
            writeWorkbook();
        } catch (Throwable failure) {
            // Never evaluate formulas/macros or emit workbook data in a crash dump.
            System.err.println("Legacy XLS operation failed (" + failure.getClass().getSimpleName() + ")");
            System.exit(1);
        }
    }
}
