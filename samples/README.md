# Samples

Small drawings bundled so you can try the system without the Stoa dataset. `questions.jsonl` holds a few
questions about them with the expected answers (Swiss and FZK-Haus: from the dataset's own question set; DfE school:
see the table); paths are relative to this
folder, so `python -m stoa.cli answer --in samples/questions.jsonl --out answers.jsonl` answers them all.

| File | What | Source and licence |
|---|---|---|
| `swiss/plan.pdf`, `.dxf`, `.png` | One apartment floor (vector PDF, CAD file and raster image of the same plan) | Swiss Dwellings v3.0.0 by Matthias Standfest, Michael Franzen, Yvonne Schröder, Luis Gonzalez Medina, Yarilo Villanueva Hernandez, Jan Hendrik Buck, Yen-Ling Tan, Milena Niedzwiecka, Rachele Colmegna (Archilyse AG), [doi:10.5281/zenodo.7788422](https://doi.org/10.5281/zenodo.7788422), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Drawing rendered by Stoa from the dataset geometry for the Junction 2026 dataset (derived work: drawing style, labels, language and numbering added; file `site117_bldg93_floor408`). Archilyse AG does not endorse these drawings. |
| `fzk_house/fzk_house.ifc` | FZK-Haus, a two-storey house (IFC4, architecture model) | Institute for Automation and Applied Informatics (IAI) / Karlsruhe Institute of Technology (KIT), [KIT IFC Examples](https://www.ifcwiki.org/index.php?title=KIT_IFC_Examples), provided "for unrestricted use"; distributed unchanged via IFC-Bench v2 (Sylvain Hellin, TUM). |
| `dfe_school/plan.pdf` | Special School Plan Level 00 (SEN-DFE-XX-00-D-X-0201, rev C01), an A2 line-only CAD sheet at 1:200 with room labels and furniture, one of the four sheets Stoa sent as examples of their test. No room areas are printed on it: the expected areas were measured by hand at the printed scale, the counts read from the room labels | UK Department for Education, from the GOV.UK publication "A new model design for a special school" (https://assets.publishing.service.gov.uk/media/6968f7ae3f43dfed1544b0e0/SEN-DFE-XX-00-D-X-0201-Special_School_Plan_Level_00-A-C01.pdf), unchanged. Contains public sector information licensed under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/). The sheet is a sample design; its site is fictitious. |

Try (from the repo root; the CLI reads relative paths against the dataset root, so pass an absolute path):

```bash
python -m stoa.cli ask "$PWD"/samples/swiss/plan.pdf "How many doors are drawn on this floor plan?"
python -m stoa.cli ask "$PWD"/samples/fzk_house/fzk_house.ifc "How many windows are in the building?"
python -m stoa.cli ask "$PWD"/samples/dfe_school/plan.pdf "What is the area of the laundry?"
```
