// F3 order-list question rules: `node --test web/src/features/orderlist/question.test.ts` (Node >= 22.18 strips the
// types). Run by tests/test_feat_orderlist.py. Pure module only.
import assert from "node:assert/strict";
import { test } from "node:test";

const { orderTopic, stockFromQ, wasteFromQ, piecesFor, roomsNamed } = await import(String(new URL("./question.ts", import.meta.url)));

test("questions the order list answered before still go to it", () => {
  for (const q of ["How much skirting should we order?", "How many pieces of skirting do we need?",
    "How much baseboard do we need to buy?", "Paljonko jalkalistaa pitää tilata?", "Wie viele Sockelleisten bestellen?",
    "How many stock lengths of skirting for the Music Room?", "Skirting kpl?"]) assert.equal(orderTopic(q), "skirting", q);
  assert.equal(orderTopic("How many doors do we need to order and in what sizes?"), "doors");
  assert.equal(orderTopic("How many square metres of vinyl should we order?"), "floor");
  assert.equal(orderTopic("What floor covering do we need to buy?"), "floor");
});

test("counting skirting BOARDS / lengths is an order now", () => {
  for (const q of ["How many skirting boards do we need?", "How many skirting boards for the Music Room?",
    "How many 3 m skirting boards do we need?", "How many 2.4 m lengths of skirting?", "How many lengths of skirting do I need?",
    "How many boards of skirting are needed?", "How many timber skirting boards?", "How many 3-metre boards of skirting?",
    "Kuinka monta jalkalistaa tarvitaan?", "how many baseboards should I get"]) assert.equal(orderTopic(q), "skirting", q);
});

test("lengths, room counts and other topics stay with the usual readers", () => {
  for (const q of ["How many metres of skirting does the D&T Food Room need?", "How many linear metres of skirting are there?",
    "How many meters of baseboard in the kitchen?", "How many rooms need skirting?", "What is the total skirting length?",
    "How long is the skirting in the Hall?", "How many skirting metres in the hall?", "How many m of skirting?",
    "Kuinka monta metriä jalkalistaa?", "In what order are the doors numbered?", "How many doors are there?",
    "What is the floor area of the kitchen?", "How much flooring is in room 101?"]) assert.equal(orderTopic(q), null, q);
});

test("a stock length the question names", () => {
  const cases: [string, number | null][] = [
    ["How many 3 m skirting boards do we need?", 3], ["How many 2.7 m long boards?", 2.7], ["How many 3-metre lengths of skirting?", 3],
    ["How many 2400 mm pieces of skirting?", 2.4], ["skirting boards of 3 m: how many?", 3], ["stock length 2,7 m: how many pieces?", 2.7],
    ["How much skirting should we order in lengths of 3 m?", 3], ["How many 3 metrin jalkalistoja?", 3],
    ["How much skirting should we order?", null], ["How many skirting boards for a 3 m wide room?", null],
    ["How many pieces of skirting with 15 % waste?", null], ["How many 30 m boards?", null]];
  for (const [q, v] of cases) assert.equal(stockFromQ(q), v, q);
});

test("a waste allowance the question names", () => {
  const cases: [string, number | null][] = [
    ["How many skirting boards with 15 % waste?", 15], ["How much skirting to order, 5% extra?", 5], ["waste of 12 %: how many boards?", 12],
    ["How many skirting boards with no waste?", 0], ["How many 3 m skirting boards do we need?", null], ["How many boards, 80 % waste?", null]];
  for (const [q, v] of cases) assert.equal(wasteFromQ(q), v, q);
});

test("pieces per room as the Python order list rounds them", () => {
  assert.deepEqual(piecesFor(8.36, 2.4, 10), { with_waste_m: 9.2, pieces: 4 });
  assert.deepEqual(piecesFor(8.36, 3, 10), { with_waste_m: 9.2, pieces: 4 });
  assert.deepEqual(piecesFor(44.13, 3, 10), { with_waste_m: 48.54, pieces: 17 });
  assert.deepEqual(piecesFor(0.2, 2.4, 10), { with_waste_m: 0.22, pieces: 1 });
  assert.deepEqual(piecesFor(4.8, 2.4, 0), { with_waste_m: 4.8, pieces: 2 });
});

test("rooms a question names by their printed name", () => {
  const ls = ["Accessible Toilet", "Corridor (1/2)", "Corridor (2/2)", "D&T Food Room", "Food Room", "Group Room (1/5)", "WC", "Hall"];
  assert.deepEqual(roomsNamed(ls, "How many skirting boards does the D&T Food Room need?"), ["D&T Food Room"]);
  assert.deepEqual(roomsNamed(ls, "How many skirting boards for the food room?"), ["Food Room"]);
  assert.deepEqual(roomsNamed(ls, "How many 3 m boards for the corridor?"), ["Corridor (1/2)", "Corridor (2/2)"]);
  assert.deepEqual(roomsNamed(ls, "How many skirting boards for the Hall and the Accessible Toilet?"), ["Accessible Toilet", "Hall"]);
  assert.deepEqual(roomsNamed(ls, "How many skirting boards do we need?"), []);
  assert.deepEqual(roomsNamed(ls, "How many skirting boards in the WC?"), []);
  assert.deepEqual(roomsNamed(ls, "skirting for the hallway"), []);
});

test("Finnish, Swedish, German and other everyday ways to count skirting pieces", () => {
  for (const q of ["Kuinka monta 2,4 metrin jalkalistaa tarvitaan?", "Montako jalkalistaa tarvitaan?", "Hur många sockellister behöver vi?",
    "Wie viele Fußleisten brauchen wir?", "Wie viele Fussleisten bestellen?", "Number of skirting boards needed?",
    "the number of 3 m skirting boards", "How many skirting boards do the classrooms need?"]) assert.equal(orderTopic(q), "skirting", q);
  for (const q of ["Paljonko jalkalistaa tarvitaan?", "Kuinka monta metriä jalkalistaa tarvitaan?", "Kuinka monta huonetta tarvitsee jalkalistaa?",
    "Hur många meter sockellist?", "Wie viele Meter Sockelleiste?", "Number of rooms with skirting?", "Count the rooms that need skirting"])
    assert.equal(orderTopic(q), null, q);
  assert.equal(stockFromQ("Kuinka monta 2,4 metrin jalkalistaa tarvitaan?"), 2.4);
  assert.equal(stockFromQ("How many skirting boards (3 m) do we need?"), 3);
  assert.equal(stockFromQ("How many boards of skirting at 3m each?"), 3);
  assert.equal(stockFromQ("How many skirting boards for a room of 3 m by 4 m?"), null);
});
