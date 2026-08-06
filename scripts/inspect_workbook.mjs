import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const [inputPath, outputDir] = process.argv.slice(2);
if (!inputPath || !outputDir) {
  throw new Error("usage: inspect_workbook.mjs <input.xlsx> <render-dir>");
}
await fs.mkdir(outputDir, { recursive: true });
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(inputPath));
const sheets = await workbook.inspect({
  kind: "sheet",
  include: "id,name",
  maxChars: 4000,
});
console.log("SHEETS");
console.log(sheets.ndjson);
const overview = await workbook.inspect({
  kind: "workbook,sheet,table",
  maxChars: 10000,
  tableMaxRows: 12,
  tableMaxCols: 12,
  tableMaxCellChars: 120,
});
console.log("OVERVIEW");
console.log(overview.ndjson);
const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
});
console.log("ERRORS");
console.log(errors.ndjson);
const names = [];
for (const line of sheets.ndjson.split("\n")) {
  if (!line.trim()) continue;
  const item = JSON.parse(line);
  const name = item.name ?? item.sheetName;
  if (name) names.push(name);
}
for (const name of names) {
  const rendered = await workbook.render({
    sheetName: name,
    autoCrop: "all",
    scale: 1,
    format: "png",
  });
  const safeName = name.replace(/[^a-zA-Z0-9_-]+/g, "_");
  await fs.writeFile(
    path.join(outputDir, safeName + ".png"),
    new Uint8Array(await rendered.arrayBuffer()),
  );
}
console.log("RENDERED=" + names.length);

