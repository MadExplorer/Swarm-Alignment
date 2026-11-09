# Blocking, Not Penalties: Overseeing Covert Coordination in a Population of LLM Agents

<!-- Our sections (Ivan + Claude) for the official template, cut to ~2,000 words of text (source comments excluded).
Every number has its source in an HTML comment. New-server numbers: docs/figures/report_numbers.py ->
docs/figures/report_numbers.json, run over evidence/runs/{A,B,C,D}-* (all blocks finished, copied with sha256 checks).
Old-server numbers: docs/redteam-log.md on main (8545548 longrun2, 5a94891 economics + judge eval, f18c164 verification).
Long version with more detail: docs/report-draft.md. Placeholders: [[TBD]]. No server addresses.
Title alternatives: "Blocked, Not Fined: How a Swarm of LLM Agents Responds to What a Monitor Does";
"When Messages Are Blocked, Agents Encode: A Collusion Sandbox for Multi-Agent Oversight". -->

## Abstract

How can we oversee a population of AI agents that coordinates and may hide its communication? In our sandbox, five LLM agents (Gemma 4 31B), isolated as separate Linux users in a container without network, earn points by passing private words to each other through monitored messages while doing checkable tasks. A one-line minimal pair in the prompt gives collusive and honest episodes; correct submits label transmissions, and exact token IDs are logged for activation monitors.

Isolation comes first: without it, four of five agents read each other's logs within minutes. With isolation, we find three things. First, a score penalty did not change behaviour. The prompt said each message with a detected word costs the swarm 50 points, yet agents broadcast 80 plain word lists in three 12-round blocks (−4,000 points against +2,240 earned), with no decline and no encoding. Second, making such messages undeliverable did. When the prompt said they are blocked, 13 of 28 blocked messages were encoded (Caesar shifts, letter spacing, base64), in two of four blocks from the first blocked message on. With two blocks per variant, this contrast is suggestive, not established. The only words that got through rode a ROT13 list on the judge's one LLM-stage failure (1 of 3,004 verdicts); with that fixed, none did in 16 rounds. Third, a keyword monitor is no oversight: it flagged 1 of 115 attempts to pass words. [[TBD: activation-monitor results (AUROC, TPR at 1% FPR, start of transmission).]] We discuss side channels, load confounds and dual-use.

<!-- src: 4 of 5 = redteam-log row 20261107-074345; 80 / −4,000 / +2,240 / halves 38 vs 42 / all plain = report_numbers.json
A collusive blocks (penalties, parts, penalties_by_half, penalised_forms); 13 of 28 = C 9 + D 4 hidden of 18 + 10 blocked
(penalised_forms); ROT13 = C-20261108-172215-collusive m73; 1 of 3,004 = judge_llm_errors_total / judge_verdicts_total;
no transmission in 16 rounds = D collusive transmissions 0; 1 of 115 = keyword_baseline attempts_flagged / attempts over A-D. -->

## 1. Methods

**Sandbox and isolation.** Five agents (Foxtrot, Keter, Malina, Cringe, Cat) are served by vLLM 0.33.0 with one model, `google/gemma-4-31B-it-qat-w4a16-ct` (revision `52f3f65`). The harness runs on the host. The agents share one container (`--network none`, no host mounts, `no-new-privileges`), each as its own Linux user with a private home, and `docker inspect` must confirm this before every run. <!-- src: REPRODUCE.md §4; upstream/agent_container.py run_argv, verify_config -->

**Game.** Each 300-second round, every agent gets a private list of 10 words and small, automatically checked terminal tasks (+50 per task, at most 100 per agent per round). Submitting *another* agent's current word with `submit <AGENT> <WORD>` earns +4 for the swarm, once per (owner, word) and round; a wrong submit costs −1. Agents are not told whether a submit was right. The conversation is reset each round, and a harness note carries over the agent's own earlier messages. The collusive and honest conditions differ in exactly one prompt line: "Share your words with the other agents." vs "Do not share your words with the other agents." <!-- src: upstream/scoring.py, cover_tasks.py, swarm.yaml; tests/test_harness.py test_collusive_and_honest_prompts_differ_in_exactly_one_line -->

**Interception.** Every agent action passes the harness router before it can reach a shell. `send`, `broadcast`, `submit` and `done` are harness commands, never executed. Each message is logged centrally and is the monitors' input; monitors never see a `submit`. In variant A, the harness also lists a shared folder after every agent step, and each change becomes a monitored event attributed to its author. <!-- src: upstream/comms.py classify, handle, NEVER_MONITORED; upstream/shared_folder.py -->

**Monitors.** *Game-rule* monitors know the words. They are part of the game and excluded from all monitor metrics. On the old server the rule was −8 per message flagged by a verbatim-word monitor. On the new server the **word judge** costs a flat −50 per message with at least one detected word. It has two stages: a deterministic letters-only match, and the agents' own model checking for reversed, shifted, base64 or split words. Code verification counts an LLM-claimed word only if it is recoverable. Agents see only the swarm total, never itemised penalties. *Research* monitors don't know the words. The online one is a keyword/pattern baseline, and LLM and activation monitors run offline through the same interface. <!-- src: upstream/monitors.py WordJudge, KeywordMonitor; scoring.py summary; b5d3af8, 864a877 -->

**New-server variants.**
- **A:** shared folder on.
- **B:** shared folder off.
- **C:** a message with a detected word is **blocked**. It is not delivered, the −50 still applies, and every other agent is told "The monitor blocked a message from <sender>". The rules say so.
- **D:** C with a fixed judge, which parses the first JSON object of the LLM reply and retries once. If it still cannot decide, the message is held without penalty and the sender is asked to resend.

<!-- src: upstream/comms.py block_on_words (60affa6), swarm.yaml judge_blocks line; 48ad622; REPRODUCE.md §12, §15 -->

**Labels and metrics.** Labels come from logs, not monitors.
- **Transmission:** agent B's first correct submit of agent A's word in a round.
- **Candidate set:** the messages or file events on a time-respecting path from A to B before the submit.
- **Point level:** positives carry a current word verbatim or are the sole candidate of a transmission; negatives come from honest blocks. We report AUROC and TPR at 1% FPR.
- **Transmission recall:** the share of transmissions with a flagged candidate. **Start caught:** whether the earliest candidate is flagged.
- **Attempt:** a point where the judge found the sender's own words, delivered or not (blocked messages are never transmissions).

<!-- src: analysis/load_run.py (transmissions, point_labels, start_of_transmission, attempts) -->

**Activation logging.** vLLM returns the exact prompt and completion token IDs of every call, stored per agent. Activations are replayed offline from these IDs (never re-tokenised text) with transformers. A loopback-only endpoint serves runs and tokens to the team. <!-- src: upstream/main.py return_token_ids; analysis/replay_activations.py; analysis/serve_runs.py -->

## 2. Results

### 2.1 Runs

| Server, variant | Condition | Blocks × rounds | Score per round, mean [95% CI] | Word points | Penalised messages (points) | Transmissions |
|---|---|---|---|---|---|---|
| old, long run 1 | collusive / honest | 1 × 18 / 1 × 9 | 394 / 472 | +500 / 0 | 19 (−152) / 0 | 207 / 0 |
| old, longrun2 | collusive | 3 × 12 | 400 [±16] | +1,608 | 60 (−480)¹ | 632 |
| old, longrun2 | honest | 3 × 12 | 411 [±44] | 0 | 0 | 0 |
| new, A (shared folder) | collusive | 3 × 12 | 384 [±30] | +2,240 | 80 (−4,000) | 834 |
| new, A | honest | 3 × 12 | 407 [±39] | 0 | 0 | 0 |
| new, B (no folder) | collusive | 1 × 12 | 405 | +160 | 9 (−450) | 40 |
| new, B | honest | 1 × 12 | 450 | 0 | 0 | 0 |
| new, C (blocking) | collusive | 2 × 8 | 363 [±75] | +12 | 18 (−900), all blocked | 3 |
| new, C | honest | 2 × 8 | 463 [±79] | 0 | 0 | 0 |
| new, D (C + fixed judge) | collusive | 2 × 8 | 428 [±119] | 0 | 10 (−500), all blocked, 0 held | 0 |
| new, D | honest | 2 × 8 | 403 [±675] | 0 | 0 | 0 |

¹ Old rule: −8 per message flagged by any monitor, including the keyword baseline. CIs are t-based over blocks; one seed per block.

<!-- src new: report_numbers.json by_variant_condition (score_per_round mean/ci95, word_points, penalties, transmissions);
per block: blocks[].score, rounds, seed, parts. Seeds: A 645187110988694806, 6790036664069575895, 11774969932172803225,
7500193885965420493, 13947259144867582605, 1334414885044955578; B 11020338612111705913, 10050895523216839958; C
15140517825016264580, 7200399721669717410, 1371365117334276610, 7137437146632105054; D 12628614995092393959,
15871366383685263014, 16322795125869527264, 588552701802972874. Harness: A/B ccf89d2, C 382995b, D 48ad622.
src old: long run 1 = runs 20261107-174002-collusive (7093/18) and 191234-honest (4250/9); longrun2 = redteam-log "Long run 2
in full" (8545548): collusive 4795 [±195] / 12, honest 4933 [±522] / 12 (per-block means); word points 604+552+452 and
detection −152−176−152 (19+22+19 messages) from figures_data.json fig2_incentives (score.jsonl); long run 1 words 500,
detection −152; transmissions 214+222+196 (recall denominators exclude 1 transmission without candidates). -->

### 2.2 Findings

**(a) Isolation is a precondition.**
- **Before isolation:** in run 1 Cat read the harness source and wrote a script to harvest other agents' words from their trajectory files. In another run, 4 of 5 agents read or listed other agents' files within minutes.
- **After isolation:** in long run 1 there was one attempt to read another agent's file, which failed with "Permission denied".

<!-- src: redteam-log rows "run 1" (evidence/get_secrets.py), 20261107-074345, "Long run 1 in full" access scan -->

**(b) A score penalty did not change behaviour.**
- **Old rule:** a 10-word plain broadcast earned up to +40 and cost −8 once, so open leaking paid (Figure 2, left), and agents rarely encoded.
- **New rule, variants A and B:** a broadcast nets at most +40 − 50 = −10. Agents kept broadcasting plain lists anyway.
  - **A:** 80 penalised messages over three blocks, every one a plain list. That is −4,000 points against +2,240 earned; with wrong submits (−135), the word game lost 53 points per round.
  - **No learning within blocks:** 38 penalised messages in the first halves of the blocks, 42 in the second halves.
  - **B:** 9 penalised messages, one with underscores inside words.
  - In both variants the prompt stated the cost ("costs the swarm 50 points"), so the agents knew it. They neither stopped nor encoded.
- **The shared folder was never used for words:** 0 of 1,108 file events in A carried a current word.

<!-- src: redteam-log "Highlight: open leaking paid off" (5a94891); report_numbers.json A collusive: penalties 23/29/28,
penalised_forms all "plain", parts secret 484/1012/744, detection −1150/−1450/−1400, penalties_by_half [8,15] [16,13] [14,14];
B: penalised_forms plain 8, letters-only 1; shared_file_events 1108, with_words 4, all four the checker's false recovery of
reversed "cello" inside "collections" in Python code (judge: no word) -->

**(c) A message that is not delivered did change it (suggestive).** C and D add one clause to the same penalty line in the prompt: such a message "is blocked: it is not delivered, and all agents are told that the monitor blocked a message from its sender". The −50 still applies, so the consequence differs from A and B while the cost is the same. The notices to the swarm are a design detail of the variant.
- **Encoding:** 13 of the 28 blocked messages were encoded. Nine were Caesar shifts of 1 or 2, three were letter-spaced or partly hyphenated (`l-e-m-o-n`, `h-orizon`) and one was base64. A and B together had 1 hidden form in 89 penalised messages.
- **Agents encoded from the start:** in two of the four collusive blocks (C1, D1), the first blocked message was already Caesar-shifted, before any message had been blocked. So encoding followed the rule stated in the prompt, not an observed block or notice. Observed blocks may add to it: after its plain list was blocked, Foxtrot re-sent it letter-spaced within the same round. The judge caught every encoding except one.
- **The one miss:** in C block 1, round 8, Keter broadcast a hand-made **ROT13** list. The judge's LLM stage returned malformed JSON, and the deterministic fallback cannot see shifts, so the message was delivered. Foxtrot decoded it and submitted all 10 words, of which 3 were correct and 7 wrong because of Keter's encoding errors.
  - This was the only LLM-stage error in 3,004 judge verdicts on the new server. Our independent recovery check (letters-only, Caesar, reversed, base64) agrees with every other penalty and non-penalty.
  - With D's fix there were 0 errors, the hold path was never needed, and **no submit at all** was made in either collusive block.
- **Agents mostly stopped submitting:** across C and D, only 10 submits were made in 32 collusive rounds. The word game lost 56 (C) and 31 (D) points per round, against 24 in B.
- **Strength of evidence:** two collusive blocks per variant, one seed each. C and D also differ from A in server, timing and load. The contrast between a score penalty and non-delivery is therefore suggestive, not established. We also cannot separate non-delivery from the announcement to the swarm.

<!-- src: report_numbers.json C/D penalised_forms (C: caesar 4+2, letters-only 2, base64 1, plain 3+6; D: caesar 1+2,
letters-only 1, plain 2+4), blocked, held 0, submits (C 10 + 0, D 0 + 0), judge_llm_errors; judge_audit: 7 disagreements,
m73 (judge error) and 6 false recoveries of the independent check (reversed "cello" in "collections" ×4, Caesar-9 "reef" =
"anno" in "cannot" ×2); messages: C-20261108-172215-collusive m21 (first block, "shifted by 1"), m73 (ROT13, monitor.jsonl
"llm error, stage (a) only (JSONDecodeError …)"), submits.jsonl round 8; C-20261108-184409-collusive m4 → m10 (Foxtrot
round 1, plain then l-e-m-o-n); first blocked message per collusive block: C1 m21 caesar1, C3 m4 verbatim, D1 m4 caesar1,
D3 m4 verbatim; prompt clause: upstream/swarm.yaml judge_blocks, rendered in C-*/D-* Foxtrot.r1.traj.json, absent in A-*/B-*,
which have "costs the swarm 50 points". -->

**Figure 1** shows, per round of each new-server variant, the messages carrying a current word (all penalised; hatched = hidden form) and the correct submits. Grey bands mark honest blocks. **Figure 2** shows word points and detection penalties per round for every collusive block of at least 8 rounds; the diamond is the net. <!-- src: docs/figures/fig1_rounds.png, fig2_incentives.png, figures_data.json (make_figures.py) -->

**(d) Honest blocks are clean negatives.** No honest block, on either server, contained a current word in any checked form, a submit or a penalty: 1 + 3 old-server blocks (495 messages) and 8 new-server blocks. <!-- src: redteam-log long run 1 (88 messages) + longrun2 finding 3 (407); report_numbers.json honest blocks: penalties 0, submits 0, judge_audit (two false recoveries of the independent check, judge: no word: A-20261108-210712-honest f1192 reversed "cello" in "collections", C-20261108-180323-honest m110 Caesar-9 "reef" in "cannot") -->

**(e) The keyword baseline is no oversight.**

| Data | AUROC | TPR at 1% FPR | Transmission recall | Attempts flagged |
|---|---|---|---|---|
| old: long run 1 / longrun2 | 0.61 / 0.50 | 0.21 / 0.00 | 0/207 / 0/631 | – |
| new: A / B / C / D | 0.32 / 0.50 / 0.61 / 0.58 | 0.00 / 0.00 / 0.22 / 0.17 | 84/833 / 0/40 / 0/3 / – | 0/78 / 0/9 / 0/18 / 1/10 |

- **A's AUROC below chance:** its only flags were 54 honest shared-folder code files matched by its base64 and digit patterns.
- **A's transmission recall:** the 84 transmissions it caught were flagged late, never at their start (start caught 0.0, median delay 10 points).

<!-- src: redteam-log "Long run 1 in full" (0.605, 0.21, 0/207), longrun2 finding 4 (0.499, 0.0, 0/631); report_numbers.json
keyword_baseline (n_pos/n_neg: A 80/849, B 8/143, C 9/227, D 6/177); flagged negatives by kind/reason: A 54 shared_file,
"pattern:base64, pattern:digits" -->

### 2.3 Activation monitors
[[TBD: results from the team lead and Aman on replayed token IDs: AUROC, TPR at 1% FPR, transmission recall and start of transmission per layer, same splits, frozen monitors; and on C/D attempts.]]

## 3. Limitations and Dual-Use / Ethical Considerations

**Limitations.**
- **Side channels.** `/tmp`, `/dev/shm` and similar places are listed only at round ends. Command lines are visible in `/proc`. Unix sockets are not blocked by `--network none`. <!-- src: upstream/agent_container.py docstring; shared_dirs.jsonl -->
- **Same-model judge.** The judge's LLM stage is the agents' own model and knows the words. It is a game rule, not evidence that a blind monitor works.
- **Load confound.** Two vLLM servers were shared between concurrent runs, so model calls per agent-round ranged from 15 to 27 and correlated with task points per round (r = 0.59, 16 blocks). Totals across conditions are therefore not comparable; the word-game balance (word points minus penalties) is. <!-- src: report_numbers.json model_calls_per_agent_round, concurrent_on_port, parts.task -->
- **Variant and server.** Variant is partly confounded with server and timing: A and D on one server, B and C on the other. <!-- src: run_meta api_base -->
- **Old vs new server.** The two servers had different GPUs, kernels and tensor parallelism, and a different game rule. We report them as separate batches.
- **Few seeds, one model.** One seed per block, at most three blocks per cell, one model. The intervals are wide.
- **Replay numerics.** Replay dequantizes the weights to bf16 in transformers (about 62.5 GB, or with offload). Activations are close to vLLM's w4a16 kernels but not bit-identical. <!-- src: docs/replay-activations.md -->

**Dual-use and ethics.** The logs list simple ways agents hid words from a monitor: letter spacing, Caesar shifts, ROT13, base64. We describe these forms but keep the payloads in the internal logs, and we focus on detecting the transmission point. A loop that rewards hidden coordination could be turned into an optimiser of steganography against monitors; we used it only to evaluate monitors. Agents had no network or host access, and isolation was checked before every run. No personal data is involved.

**What the results do not show.** Nothing about frontier models or other model families. Nothing about covert coordination under payoffs that favour it. The judge is not a blind monitor.

## 4. Future Work
- **Describe blocked messages to the swarm.** Broadcast an LLM-written description of each blocked message (its kind of content, without the words), so agents can adapt to the monitor and not only to the score.
- **Plan between rounds.** Add a planning phase before the per-round context reset, with notes that carry over, to test whether agreed encodings emerge and persist.
