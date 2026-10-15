# Human-review rubric v1 (not yet applied)

All questions are `draft_not_human_reviewed`. Programmatic citation checks are
not semantic accuracy. Keep these human judgments null until an identified
reviewer actually checks the PDFs and the complete answer.

1. **Answer correctness:** 0 = wrong/unsupported; 1 = partly correct or missing
   necessary qualifications; 2 = complete and faithful to relevant pages.
   Compare against the draft reference; correct the reference if the PDF shows
   it is wrong. Record reviewer ID, UTC review time, rubric version and notes.
2. **Citation support:** for each substantive factual claim, mark supported,
   partially supported, unsupported, or not factual. Check the displayed excerpt
   and original PDF page, not just whether a source ID exists. Aggregate supported
   factual claims / factual claims; do not count empty answers as perfect.
3. **Unsupported-claim rate:** unsupported factual claims / all factual claims;
   report denominator and count, null when no factual claims.
4. **Abstention:** an absent fact should elicit insufficient evidence, not an
   invented answer. The ambiguous visitor-limit question also permits a response
   explicitly asking which organization or carefully presenting both limits;
   the programmatic binary abstention metric cannot recognize that nuance.
5. **Conflict:** a complete answer identifies both fee amounts and the unresolved
   authority. Picking one amount without noting disagreement is incorrect.
6. **Multi-document questions:** check that every required fact is present and
   separately supported, rather than rewarding a single valid citation.

Review all twelve references before using scores for model selection. Use dev
for tuning. Freeze providers, prompt/config and corpus before one heldout run.
Any repeated heldout inspection invalidates its status as an untouched holdout.
A future optional model judge must record its model, rubric and prompt versions,
label results model-judged, and receive a human audit; none is implemented here.
