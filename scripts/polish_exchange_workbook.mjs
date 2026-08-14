import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const [inputPath, outputPath, renderDir] = process.argv.slice(2);
if (!inputPath || !outputPath || !renderDir) {
  throw new Error("usage: polish_exchange_workbook.mjs <input.xlsx> <output.xlsx> <render-dir>");
}
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(inputPath));
const first = workbook.worksheets.getItemAt(0);
const second = workbook.worksheets.getItemAt(1);

function cleanText(value) {
  return typeof value === "string" ? value.replaceAll("**", "").trim() : value;
}
function numberOrText(value) {
  const clean = cleanText(value);
  if (clean === "N/A" || clean === "") return clean;
  const parsed = Number(String(clean).replace(/[^0-9.-]/g, ""));
  return Number.isFinite(parsed) ? parsed : clean;
}
function styleHeader(sheet, address) {
  sheet.getRange(address).format = {
    fill: "#17365D",
    font: { bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "all", style: "thin", color: "#9FBAD0" },
  };
}
function styleBody(sheet, address) {
  sheet.getRange(address).format = {
    borders: { preset: "all", style: "thin", color: "#D9E2F3" },
    verticalAlignment: "center",
  };
  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(1);
}

const firstValues = first.getRange("A1:E4").values;
const cleanedFirst = firstValues.map((row, rowIndex) =>
  row.map((value, colIndex) => {
    if (rowIndex === 0 || colIndex === 0 || colIndex === 4) return cleanText(value);
    return numberOrText(value);
  }),
);
first.getRange("A1:E4").values = cleanedFirst;
styleHeader(first, "A1:E1");
styleBody(first, "A2:E4");
first.getRange("B2:D4").format.numberFormat = '#,##0.000" KRW"';
first.getRange("A1:A4").format.columnWidth = 18;
first.getRange("B1:D4").format.columnWidth = 22;
first.getRange("E1:E4").format.columnWidth = 25;
first.getRange("A1:E4").format.rowHeight = 24;

const secondValues = second.getRange("A1:I16").values;
const cleanedSecond = secondValues.map((row, rowIndex) =>
  row.map((value, colIndex) => {
    if (rowIndex === 0 || colIndex === 2) return cleanText(value);
    return numberOrText(value);
  }),
);
second.getRange("A1:I16").values = cleanedSecond;
styleHeader(second, "A1:I1");
styleBody(second, "A2:I16");
second.getRange("A2:B16").format.numberFormat = "#,##0";
second.getRange("D2:E16").format.numberFormat = '#,##0.000" KRW"';
second.getRange("F2:I16").format.numberFormat = "0.00%";
second.getRange("A1:B16").format.columnWidth = 12;
second.getRange("C1:C16").format.columnWidth = 28;
second.getRange("D1:E16").format.columnWidth = 16;
second.getRange("F1:I16").format.columnWidth = 22;
second.getRange("A1:I16").format.rowHeight = 24;

const guide = workbook.worksheets.add("Guide");
guide.getRange("A1:D1").merge();
guide.getRange("A1").values = [["Exchange FX Decision Support Result"]];
guide.getRange("A1:D1").format = {
  fill: "#17365D",
  font: { bold: true, color: "#FFFFFF", size: 16 },
  verticalAlignment: "center",
};
guide.getRange("A3:B10").values = [
  ["Item", "Details"],
  ["Purpose", "Research and decision support for FX trading judgment"],
  ["Execution scope", "Verified Exchange internal evidence only; no public search"],
  ["Safety boundary", "No trading, account connection, personalized orders, or profit promise"],
  ["Data limitation", "Read each source observation date and freshness limitation"],
  ["Verification", "Repository tests 25/25, transfer integrity 33/33, independent review PASS"],
  ["Actual Gemini cost", "$0.201832"],
  ["Original result", "See final.md and closure_report.json"],
];
styleHeader(guide, "A3:B3");
styleBody(guide, "A4:B10");
guide.getRange("A1:D1").format.rowHeight = 32;
guide.getRange("A1:A10").format.columnWidth = 24;
guide.getRange("B1:B10").format.columnWidth = 74;
guide.getRange("B4:B10").format.wrapText = true;
guide.getRange("A3:B10").format.rowHeight = 28;
guide.showGridLines = false;
guide.freezePanes.freezeRows(3);

await fs.mkdir(new URL(".", "file:///" + outputPath.replaceAll("\\", "/")).pathname, { recursive: true }).catch(() => {});
const exported = await SpreadsheetFile.exportXlsx(workbook);
await exported.save(outputPath);
await fs.mkdir(renderDir, { recursive: true });
for (const [sheet, safe] of [[first, "horizon"], [second, "annual"], [guide, "guide"]]) {
  const rendered = await workbook.render({ sheetName: sheet.name, autoCrop: "all", scale: 1, format: "png" });
  await fs.writeFile(renderDir + "/" + safe + ".png", new Uint8Array(await rendered.arrayBuffer()));
}
const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
});
console.log(errors.ndjson);
console.log((await workbook.inspect({ kind: "workbook,sheet,table", maxChars: 12000, tableMaxRows: 18, tableMaxCols: 10 })).ndjson);

