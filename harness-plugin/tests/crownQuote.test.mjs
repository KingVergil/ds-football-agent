/**
 * 斗狗场订单行的「皇冠水位」标注。
 *
 * 引擎订单记的是 Pinnacle 终盘水位（实际常下在皇冠），要在待投订单行旁标出皇冠同侧水位。
 * 这里钉住取数口径：只读 tags 里 asian-handicap-crown / over-under-crown 段的**末点**，
 * 亚盘 h=主/a=客，大小球 大=首段/小=末二段，盘口换算成主队视角（负=主让）。
 */
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { crownQuote } from "../dashboard.js";

function fixture(sections) {
  const dir = mkdtempSync(join(tmpdir(), "dsd-crown-"));
  mkdirSync(join(dir, "tags"), { recursive: true });
  writeFileSync(
    join(dir, "tags", "Lota1.json"),
    JSON.stringify({ lota_id: "Lota1", sections }),
    "utf8",
  );
  return dir;
}

const AH_CROWN = [
  "亚盘:Crown t=Δt±m odds=h/handicap/a/r(rrr%)",
  "OPt-18973m=0.82/一/半/1.06(r96.63%)",
  "Δt+147m↓→↑↓0.84/半/二/1.05/96.97",
  "Δt+122m↑→↓↑0.85/半/二/1.04/97.02",
].join("\n");

test("亚盘主队侧：取末点 h，盘口换算成主队视角（半/二 → -1.75）", () => {
  const dir = fixture({ "asian-handicap-crown": AH_CROWN });
  assert.deepEqual(crownQuote(dir, "Lota1", "亚盘", "H"), { odds: 0.85, handicap: -1.75 });
});

test("亚盘客队侧：取末点 a", () => {
  const dir = fixture({ "asian-handicap-crown": AH_CROWN });
  assert.deepEqual(crownQuote(dir, "Lota1", "亚盘", "A"), { odds: 1.04, handicap: -1.75 });
});

test("受让盘口取反：受一球 → +1", () => {
  const dir = fixture({
    "asian-handicap-crown": [
      "亚盘:Crown t=Δt±m odds=h/handicap/a/r(rrr%)",
      "Δt+68m↑→↓↑1.11/受一球/0.79/96.84",
    ].join("\n"),
  });
  assert.deepEqual(crownQuote(dir, "Lota1", "亚盘", "A"), { odds: 0.79, handicap: 1 });
});

test("大小球：大取首段、小取末二段，阈值原样", () => {
  const dir = fixture({
    "over-under-crown": [
      "大小球:Crown t=Δt±m odds=o/handicap/u/r(rrr%)",
      "Δt+127m↑→↓↑0.89/3/1.00/97.17",
    ].join("\n"),
  });
  assert.deepEqual(crownQuote(dir, "Lota1", "大小球", "over"), { odds: 0.89, handicap: 3 });
  assert.deepEqual(crownQuote(dir, "Lota1", "大小球", "under"), { odds: 1, handicap: 3 });
});

test("缺段 / 未缓存比赛 / 胜平负 → null", () => {
  const dir = fixture({ "asian-handicap-pinnacle": "亚盘:Pinnacle t=Δt±m odds=h/handicap/a/r(rrr%)" });
  assert.equal(crownQuote(dir, "Lota1", "亚盘", "H"), null);
  assert.equal(crownQuote(dir, "Lota9999", "亚盘", "H"), null);
  assert.equal(crownQuote(fixture({}), "Lota1", "胜平负", "H"), null);
});

test("残缺订单（bet_type 为空）不标注", () => {
  const dir = fixture({ "asian-handicap-crown": AH_CROWN });
  assert.equal(crownQuote(dir, "Lota1", "", ""), null);
  assert.equal(crownQuote(dir, "Lota1", null, null), null);
});
