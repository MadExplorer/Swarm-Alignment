# Cut plan: our part of the report, 2,912 → under 2,000 words

Counts are words without the source comments, from `docs/report-draft.md` at `c2a5c46`. Plan only: nothing has been cut yet. Every finding (a)–(f) keeps at least one sentence.

**Check the template first.** If the "Limitations and Dual-Use / Ethical Considerations" appendix is outside the 8-page limit, section 3 (~380 words after cutting) moves there, and the main text drops to ~1,600 words.

| Section | Now | Target | What goes | What stays |
|---|---|---|---|---|
| 1.1 Sandbox and isolation | 125 | 70 | The `docker inspect` sentence and the argument-list detail (code-level) | One container, `--network none`, one Linux user per agent (home 700), harness on the host, isolation checked before every run |
| 1.2 Game, rounds, conditions | 149 | 110 | The scoring parenthesis on per-round crediting; the sentence explaining why the probe can't read the instruction (keep only "exactly one line") | 10 words, +4 / −1, tasks +50 (cap 100), 300 s rounds, context reset with harness note, the minimal pair verbatim |
| 1.3 Commands and interception | 104 | 60 | The test sentence (monitors that flag / block / redact); embedded-command detail | Harness commands intercepted before the shell, central message log, submits never reach a monitor, shared folder as a second observed channel (one sentence) |
| 1.4 Monitors | 218 | 150 | Stage (a)/(b) mechanics shortened to one sentence; the old −8 rule folded into the run-table caption | Game rule vs research split (and why game rule is excluded); word judge + verification in one sentence; −50 rule; variant C blocking + notice (one sentence); keyword baseline as the online research monitor |
| 1.5 Labels and metrics | 125 | 90 | The bullet layout (one paragraph instead) | Transmission, candidate set, AUROC and TPR at 1% FPR, recall, start-of-transmission (`start_caught`, delay) |
| 1.6 Logging for replay | 82 | 50 | The endpoint details (tunnel, `/detokenize`) | Exact token IDs logged per call, offline replay of IDs (never re-tokenised), endpoint in half a sentence |
| 2.1 Run table + notes | 517 | 220 | Pilot rows merged into one; the old validation runs (163124, 172319) merged into one "validation" row; longrun2 collapsed to two rows (collusive / honest, mean [95% CI], n = 3) with per-seed values in the comment; the seed list stays in the comment only; footnote ¹ to one line | Long run 1 rows, longrun2 means, new-server validation, A/B/C rows; server and variant columns |
| 2.2 (a) isolation | 120 | 70 | The Foxtrot detail and the long-run-1 access scan sentence | Cat (env, `ls -R /`, harness source, `get_secrets.py`); Keter and Malina grepping live trajectories; 4 of 5 agents within minutes; after isolation one attempt, "Permission denied" |
| 2.2 (b) incentives | 150 | 100 | The arithmetic sentence (Figure 2 carries it) | +40 vs −8 ⇒ ≈ +32; longrun2 honest 4933 vs collusive 4795 with the −174/−500 breakdown in one sentence; −50 ⇒ −10; **Figure 2** |
| 2.2 (c) encodings | 140 | 80 | Separate bullets merged into one sentence per server | Caesar agreement (flagged agreement, not payloads); letter spacing round 11 carrying 35 submits; base64 by hand; none in longrun2; underscore form on the new server |
| 2.2 (d) A/B/C | 210 | 170 | The "agents never see penalties" sentence folded into the C sentence | A: 23 plain lists, −1150 vs +484, no decline (8 → 15), shared folder unused for words; B: 9 (1 hidden), no decline; judge exact (0 mismatches); C [[TBD]]; **Figure 1** |
| 2.2 (e) honest blocks | 60 | 35 | Per-run detail | 0 leaks, 0 submits across 1 + 3 old-server honest blocks and the new-server validation; [[TBD]] new honest blocks |
| 2.2 (f) keyword baseline | 90 | 60 | The B sentence (0/40), kept in the comment | AUROC 0.605 / 0.499, TPR at 1% FPR 0.21 / 0.0, recall 0/207, 0/631; A: recall 0.21 but `start_caught` 0 |
| Judge offline eval | 50 | 25 | Precision numbers | 0 false positives on honest; recall 0.96 / 1.00; verification 5 → 1 false positives |
| 2.3 Activation monitors | 35 | 35 | – | Placeholder (the team lead's and Aman's part has its own space) |
| 3 Limitations | 400 | 260 | Each bullet cut to one or two lines; the image change and `protected_regular` reduced to one clause; the word-list overlap to half a sentence | Side channels (`/tmp`, `/proc`, sockets), same-model judge, A/B server confound, old vs new server, few seeds / one model, replay numerics and offload |
| 3 Dual-use + what it doesn't show | 170 | 120 | Repetition between the two paragraphs | Encodings as an evasion recipe (forms described, payloads kept internal), sandbox dual-use, isolation, no personal data; not frontier models, not covert coordination under favourable payoffs, judge not blind, small n |
| 4 Future Work | 112 | 80 | Second sentence of each entry shortened | Both entries |
| Figure captions (new) | 0 | 80 | – | Two captions |
| **Total** | **2,912** | **~1,865** (≈ 135 words of slack under 2,000) | | |

**Do not cut:** any number in (a)–(f); the minimal-pair sentence; the game-rule vs research distinction; the A/B server confound; the dual-use paragraph.
