# Human verdicts on run-20261017-093112

**Luca Schwarz (@Backverbot)** reviewed the generated models of test bench run
`run-20261017-093112` on 2026-10-17. `verdicts.json` holds his verdict per part with his reasons
verbatim, in German. The test bench checks against it how well its "solved"
criteria agree with human judgement (`python -m mechatronikers.testbench agreement`, #93).

| File | Content |
|---|---|
| `verdicts.json` | Run id, reviewer, date, source and, per part, the verdict (good / poor / failed), the reasons and the model file |
| `<Part>_KI.ap242.step` | The generated model the verdict refers to (Git LFS) |

**Sources** (branch `5-validation-of-3d-model`, not merged):

- Reasons: commit `aefc822` (Luca Schwarz), `tests/model_validation_results.txt`. Each line of
  his notes is one entry in `reasons`. Only the leading `-`, the indentation and trailing spaces
  were removed.
- Labels: good / poor / failed summarise those notes, as listed in issue #93. "Welle" is good with
  minor differences (`"note": "minor"`).
- Models: commit `23ff937` (Alexander Lakay), `data/<Part>/<Part>_KI.ap242.step`, copied
  byte-identical (same LFS objects). Their export time stamps (2026-10-17 09:34–09:51) fit the
  run id. The run made no model for Maschinenteil ("Wurde nicht gerendert"), so its `model` is
  `null`.

These are evaluation fixtures, not challenge inputs: they live here, not in `data/`. They are
never used for training (docs/09).
