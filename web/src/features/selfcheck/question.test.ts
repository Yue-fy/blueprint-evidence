// F3 self-check question matcher: `node --test web/src/features/selfcheck/question.test.ts` (Node >= 22.18 strips the
// types). Run by tests/test_feat_selfcheck.py. Pure module only.
import assert from "node:assert/strict";
import { test } from "node:test";

const { checkQuestion, phrase } = await import(String(new URL("./question.ts", import.meta.url)));

test("consistency questions are the self-check's, with the checks they name", () => {
  assert.deepEqual(checkQuestion("Are there any errors on this drawing?"), []);
  assert.deepEqual(checkQuestion("Check this drawing for mistakes"), []);
  assert.deepEqual(checkQuestion("Is anything inconsistent in this sheet?"), []);
  assert.deepEqual(checkQuestion("Do the dimension chains add up?"), ["dimension_chains", "dimension_drawn"]);
  assert.deepEqual(checkQuestion("Does the area table add up to its total?"), ["area_table_sums"]);
  assert.deepEqual(checkQuestion("Does the area schedule match?"), ["area_table_sums"]);
  assert.deepEqual(checkQuestion("Does the door schedule match the plan?"), ["schedule_tags"]);
  assert.deepEqual(checkQuestion("Does the scale bar agree with the printed scale?"), ["scale_bar"]);
  assert.deepEqual(checkQuestion("Do the lifts line up between the floors?"), ["floors_align"]);
  assert.deepEqual(checkQuestion("Are there duplicate room numbers?"), ["room_tags"]);
  assert.deepEqual(checkQuestion("Does the plan match the 3D model?"), ["plan_vs_model"]);
  assert.deepEqual(checkQuestion("Onko piirustuksessa virheitä?"), []);
  assert.deepEqual(checkQuestion("Gibt es Fehler in der Bemaßung?"), ["dimension_chains", "dimension_drawn"]);
});

test("measuring / counting questions stay with the pipeline", () => {
  for (const q of ["What is the area of the Laundry?", "How many doors does the Laundry have?",
    "How much skirting should we order?", "What is the total area of all Secondary Classrooms?",
    "How many errors are in the door schedule?", "What is the scale of this drawing?", "Which rooms are on Level 01?",
    "How many metres of skirting does the Laundry need, door openings excluded?", "Where is the lift?",
    "What is the floor area of the Multi-Purpose Hall?", "Is the kitchen bigger than the laundry?",
    "Does the school have a lift?", "What does the door schedule say about door 104A?",
    // the hard split: sums asked for (they once matched 'add up' + 'floor')
    "What do the net floor areas of all spaces on '2. Level' add up to, in m²?",
    "Add up the net areas of all rooms labelled 'Zimmer' on this floor. Answer in m².",
    "Do the room areas on this floor add up to more than 100 m²?", "Sum the areas of all bedrooms."]) {
    assert.equal(checkQuestion(q), null, q);
  }
});

test("answers read the right way round on the card (Yes / No shown big)", () => {
  const f = ["Dimension chain 4975 + 4975 ≠ 9590"];
  assert.equal(phrase("Are there any errors on this drawing?", f, 7), "Yes: 1 possible error: Dimension chain 4975 + 4975 ≠ 9590");
  assert.equal(phrase("Are there any errors on this drawing?", [], 6), "No: 6 cross-checks agree, none disagree");
  assert.equal(phrase("Does the area table add up?", [], 6), "Yes: 6 cross-checks agree, none disagree");
  assert.equal(phrase("Do the dimension chains add up?", f, 7), "No: 1 possible error: Dimension chain 4975 + 4975 ≠ 9590");
  assert.equal(phrase("Is the scale bar correct?", [], 1), "Yes: 1 cross-check agrees, none disagrees");
  assert.deepEqual(checkQuestion("Do the room sizes match the drawing?"), ["room_areas"]);
  assert.ok(checkQuestion("Do the areas in the room tags match the plan?").includes("room_areas"));
  assert.equal(checkQuestion("What is the room size of the kitchen?"), null);
  assert.equal(phrase("Do the room areas match the drawing?", [], 1, "39 printed room areas match the room traced around their tag (within 5 %), e.g. Main Hall 197 m2 ≈ 199.1 ✓"),
    "Yes: 39 printed room areas match the room traced around their tag");
  assert.match(phrase("Check this drawing for mistakes", [], 3), /^No inconsistencies in the checks that could run: 3 cross-checks agree/);
  assert.match(phrase("Check this drawing for mistakes", f, 3), /^1 possible error: /);
  assert.equal(checkQuestion("Is the scale bar correct?")?.join(), "scale_bar");
});

test("completeness between two documents is a schedule / model check; one place only is not", () => {
  assert.deepEqual(checkQuestion("Are all doors in the schedule on the plans?"), ["schedule_tags"]);
  assert.deepEqual(checkQuestion("Is every door on the plan listed in the door schedule?"), ["schedule_tags"]);
  assert.deepEqual(checkQuestion("Are any doors missing from the schedule?"), ["schedule_tags"]);
  assert.deepEqual(checkQuestion("Are all rooms on the plan in the 3D model?"), ["plan_vs_model"]);
  for (const q of ["Are all doors in the schedule fire rated?", "Which doors are missing from the schedule?",
    "Are all bedrooms on level 2 larger than 10 m²?", "Is anything missing?", "Are all rooms on this floor accessible?",
    "On sheet A-801, are all doors in the schedule fire rated?",
    "On sheet A-801 of the Xcel Sports Complex drawings, what is the total area of all rooms that have an area listed in the finish room schedule?",
    "A refurbishment replaces every toilet (WC) drawn on this floor plan at 420 CHF each and every washbasin at 260 CHF each. What is the total cost?"]) {
    assert.equal(checkQuestion(q), null, q);
  }
  const f = ["Door 104C on the plan is not in the door schedule"];
  assert.match(phrase("Are any doors missing from the schedule?", f, 2), /^Yes: 1 possible error/);
  assert.match(phrase("Are any doors missing from the schedule?", [], 2), /^No: 2 cross-checks agree/);
  assert.match(phrase("Are all doors in the schedule on the plans?", f, 2), /^No: 1 possible error/);
  assert.match(phrase("Are all doors in the schedule on the plans?", [], 2), /^Yes: /);
});

test("revision (issue) questions: yes / no about the open sheet's issue only", async () => {
  const { revisionQuestion } = await import(String(new URL("./question.ts", import.meta.url)));
  for (const q of ["Is this print up to date?", "Is this the latest revision?", "Is this drawing superseded?",
    "Is this scan out of date?", "Is this the current drawing?", "Does this print show the same revision as the PDF?",
    "Is this an old issue?"]) assert.equal(revisionQuestion(q), true, q);
  for (const q of ["What revision is this drawing?", "How many doors are on this floor?", "Is the kitchen bigger than the laundry?",
    "Which rooms were changed in the latest revision?", "What is the area of the Laundry?", "Is the lift up to the latest code?",
    "Are there any errors on this drawing?"]) assert.equal(revisionQuestion(q), false, q);
});
