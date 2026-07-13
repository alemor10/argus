# Golden files

Expected digest markdown and the gate-verdict table (~30 `(field, value,
context) → (verdict, code)` rows). Byte-compared by the golden tests so any
rendering or bound change is a reviewed diff, never a silent behavior change.
Regenerated deliberately via `pytest --update-golden`.

The flagship golden digest must also pass the negative assertion: the string
"218" appears nowhere (the NTDOY fake-upside number).

Populated during the engine implementation phase.
