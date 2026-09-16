/**
 * 串关票在斗狗场「订单区块」的展示口径。
 *
 * 2026-09-13 实测的坑：9过5 票（5 双选 + 4 单选、0 全包）被显示成 "5全包+4单选"——
 * 因为 nCover 算成了「总腿数 − 单选腿数」，把所有多选腿都当全包。
 * 这里把计数口径与成本口径都钉住。
 */
import test from "node:test";
import assert from "node:assert/strict";

/** 复刻 dashboard.js 里的腿计数与文案（纯函数，便于回归）。 */
function picksDesc(legs) {
  const nSingle = legs.filter((l) => l.picks.length === 1).length;
  const nDouble = legs.filter((l) => l.picks.length === 2).length;
  const nCover = legs.filter((l) => l.picks.length >= 3).length;
  return [nCover ? `${nCover}全包` : "", nDouble ? `${nDouble}双选` : "",
          nSingle ? `${nSingle}单选` : ""].filter(Boolean).join("+");
}

test("9过5 实票：5 双选 + 4 单选，不能把双选算成全包", () => {
  const legs = [
    { picks: ["H"] }, { picks: ["H", "D"] }, { picks: ["D"] }, { picks: ["D", "A"] },
    { picks: ["H"] }, { picks: ["H", "D"] }, { picks: ["D", "A"] }, { picks: ["D"] },
    { picks: ["H", "D"] },
  ];
  assert.equal(picksDesc(legs), "5双选+4单选");
  assert.ok(!picksDesc(legs).includes("全包"), "没有三选腿就不该出现「全包」");
});

test("真·全包腿（3 选）单独计数", () => {
  const legs = [{ picks: ["H", "D", "A"] }, { picks: ["H"] }, { picks: ["H", "D"] }];
  assert.equal(picksDesc(legs), "1全包+1双选+1单选");
});

test("全单选不发散", () => {
  assert.equal(picksDesc([{ picks: ["H"] }, { picks: ["D"] }]), "2单选");
});
