import test from "node:test";
import assert from "node:assert/strict";

import {
  splitDayWindows,
  matchOffsetMinutes,
  anchorOffsetMinutes,
} from "../windows.js";

let _seq = 0;
const m = (match_time, lota_id = `L${String(++_seq).padStart(6, "0")}`) => ({ lota_id, match_time });

test("matchOffsetMinutes: 足球日起点为当日 12:00", () => {
  assert.equal(matchOffsetMinutes("2026-08-01", "2026-08-01 17:30"), 330);
  assert.equal(matchOffsetMinutes("2026-08-01", "2026-08-01 12:00"), 0);
  assert.equal(matchOffsetMinutes("2026-08-01", "2026-08-02 00:30"), 750);
  assert.equal(matchOffsetMinutes("2026-08-01", "2026-08-02 12:00"), null);
  assert.equal(matchOffsetMinutes("2026-08-01", "2026-08-01 11:59"), null);
  assert.equal(matchOffsetMinutes("2026-08-01", "bad time"), null);
});

test("anchorOffsetMinutes: 跨日锚点", () => {
  assert.equal(anchorOffsetMinutes("17:30"), 330);
  assert.equal(anchorOffsetMinutes("19:30"), 450);
  assert.equal(anchorOffsetMinutes("21:30"), 570);
  assert.equal(anchorOffsetMinutes("00:30"), 750);
});

test("≤10 场不切窗（1 窗全量）", () => {
  const matches = Array.from({ length: 10 }, (_, i) =>
    m(`2026-08-01 ${String(18 + Math.floor(i / 4)).padStart(2, "0")}:${String((i % 4) * 15).padStart(2, "0")}`));
  const wins = splitDayWindows("2026-08-01", matches);
  assert.equal(wins.length, 1);
  assert.equal(wins[0].match_ids.length, 10);
});

test(">10 场按贪心装满 10，密集批次均衡切块（无 1 场尾窗）", () => {
  const matches = [];
  for (let i = 0; i < 11; i++) {
    const hh = String(18 + Math.floor(i / 4)).padStart(2, "0");
    const mm = String((i % 4) * 15).padStart(2, "0");
    matches.push(m(`2026-08-01 ${hh}:${mm}`));
  }
  const wins = splitDayWindows("2026-08-01", matches);
  assert.equal(wins.length, 2);
  assert.deepEqual(wins.map((w) => w.match_ids.length), [6, 5]);
});

test("28 场密集批次 → 均衡切 10+10+8", () => {
  const matches = [];
  for (let i = 0; i < 28; i++) {
    const hh = String(17 + Math.floor(i / 4)).padStart(2, "0");
    const mm = String((i % 4) * 15).padStart(2, "0");
    matches.push(m(`2026-08-01 ${hh}:${mm}`));
  }
  const wins = splitDayWindows("2026-08-01", matches);
  assert.deepEqual(wins.map((w) => w.match_ids.length), [10, 10, 8]);
});

test("跨大时段（≥180min）断成独立 session", () => {
  const matches = [
    m("2026-08-01 18:30"), m("2026-08-01 18:30"), m("2026-08-01 18:30"),
    m("2026-08-01 20:00"), m("2026-08-01 21:00"), m("2026-08-01 22:00"),
    m("2026-08-01 23:00"), m("2026-08-02 00:00"), m("2026-08-02 00:00"),
    m("2026-08-02 01:00"), m("2026-08-02 02:00"),
    m("2026-08-02 09:40"),
  ];
  const wins = splitDayWindows("2026-08-01", matches);
  assert.equal(wins.length, 3, "傍晚 session（11 场均衡 6+5）+ 早场 session（1 场）");
  assert.deepEqual(wins.map((w) => w.match_ids.length), [6, 5, 1]);
  assert.ok(wins[2].anchor.includes("早场"));
  assert.equal(wins.reduce((s, w) => s + w.match_ids.length, 0), 12);
});

test("按 lota_id 去重（同场只进一个窗口）", () => {
  const matches = [
    m("2026-08-01 18:30", "L1"), m("2026-08-01 18:30", "L1"),
    m("2026-08-01 18:30", "L2"), m("2026-08-01 20:00", "L3"),
    m("2026-08-01 21:00", "L4"), m("2026-08-01 22:00", "L5"),
    m("2026-08-01 23:00", "L6"), m("2026-08-02 00:00", "L7"),
    m("2026-08-02 00:00", "L8"), m("2026-08-02 01:00", "L9"), m("2026-08-02 02:00", "L10"),
  ];
  const wins = splitDayWindows("2026-08-01", matches);
  const all = wins.flatMap((w) => w.match_ids);
  assert.equal(new Set(all).size, all.length);
  assert.equal(all.length, 10);
});

test("空列表返回空", () => {
  assert.deepEqual(splitDayWindows("2026-08-01", []), []);
});
