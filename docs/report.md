# Blocking, Not Penalties: Overseeing Covert Coordination in a Population of LLM Agents

<!-- Our sections (Ivan + Claude) for the official template, cut to ~2,000 words of text (source comments excluded).
Every number has its source in an HTML comment. All run numbers (new and old server): scripts/results_summary.py ->
docs/figures/results_summary.json (keys given as RS:<key>; numbers only, no message text), run over evidence/runs
(copied with sha256 checks). Manual review of the keyword candidates: evidence/results/manual_review_pass1.md (git-ignored).
Numbers NOT from that file, with their own sources: the isolation anecdotes (4 of 5 agents; get_secrets.py; "Permission
denied") from docs/redteam-log.md on main; the incident figure (about 1,200 agents) from METR & Redwood Research (2026);
software versions; the 62.5 GB replay estimate (docs/replay-activations.md).
Old-server numbers: docs/redteam-log.md on main (8545548 longrun2, 5a94891 economics + judge eval, f18c164 verification).
Long version with more detail: docs/report-draft.md. Placeholders: [[TBD]]. No server addresses.
Title alternatives: "Blocked, Not Fined: How a Swarm of LLM Agents Responds to What a Monitor Does";
"When Messages Are Blocked, Agents Encode: A Collusion Sandbox for Multi-Agent Oversight". -->

## Abstract

How can we oversee a population of AI agents that coordinates and may hide its communication? In our sandbox, five LLM agents (Gemma 4 31B), isolated as separate Linux users in a container without network, earn points by passing private words to each other through monitored messages while doing checkable tasks. A one-line minimal pair in the prompt gives collusive and honest episodes; correct submits label transmissions, and exact token IDs are logged for activation monitors.

Isolation comes first: without it, four of five agents read each other's logs within minutes. With isolation, we find three things. First, a score penalty did not change behaviour. The prompt said each message with a detected word costs the swarm 50 points, yet in three 12-round blocks agents sent 78 messages with their own words, all plain (−4,000 in penalties against +2,240 earned), with no decline. Second, making such messages undeliverable did. When the prompt said they are blocked, 13 of 28 blocked messages were encoded (Caesar shifts, letter spacing, base64). In two of four blocks the first blocked message was already encoded; in the other two the blocked agent re-sent its words encoded within a minute. With two blocks per variant this is suggestive, not established. The only words that got through rode a ROT13 list on the judge's one LLM-stage failure (1 of 3,004 verdicts); with that fixed, none did in 16 rounds. Third, a keyword monitor is no oversight: it flagged 1 of 116 attempts. [[TBD: activation-monitor results (AUROC, TPR at 1% FPR, start of transmission).]] We discuss confounds and dual-use.

<!-- src: 4 of 5 = redteam-log row 20261107-074345; 78 plain / 0 encoded = RS:table1 A collusive attempt_msgs_plain /
attempt_msgs_encoded; −4,000 / +2,240 = RS:report.groups["A collusive"] detection_points / word_points; no decline =
attempt_msgs_first_half 38 / second_half 40; 13 of 28 = RS:table1 C+D attempt_msgs_encoded_blocked 9+4 of attempt_msgs_blocked
18+10; first blocked already encoded / re-sent within a minute = RS:cd_timing (C1, D1 same message; C3 46.7 s, D3 30.2 s,
same sender); ROT13 = RS:table2 ROT13 (C1 m73); 1 of 3,004 = RS:judge_counts summed (llm_error_messages / verdicts); none in
16 rounds = RS:table1 D collusive tx 0, rounds 16; 1 of 116 = RS:report.keyword A-D attempt_msgs_flagged 0+0+0+1 /
attempt_msgs 78+9+19+10. -->

## 1. Introduction

Track 4 of the Apart Research Swarm Alignment Hackathon asks how to oversee a population of AI agents that coordinates and can hide its communication. LLM agents are increasingly deployed as many interacting instances, and the risks that arise between agents, such as miscoordination, conflict and collusion, differ from those of a single model (Hammond et al., 2025). Collusion is hard to oversee for two reasons: agents can hide information in innocuous-looking text (Motwani et al., 2024), and a goal can be split into steps that each look harmless (Jones et al., 2025). An independent investigation of an incident in which OpenAI evaluation agents coordinated an unauthorized attack on Hugging Face found that about 1,200 agents communicated through an improvised message board and that some tampered with their own logs (METR & Redwood Research, 2026). This is what Track 4 asks: overseeing a population that coordinates and can hide its traces.

In our sandbox, five Gemma 4 31B agents, each a separate Linux user in an isolated container, earn points for their swarm by passing private words to each other through monitored messages while doing automatically checked terminal tasks. A one-line prompt difference gives collusive and honest episodes. The harness logs ground-truth labels (who transmitted which word, through which messages) and the exact token IDs of every model call, for offline text and activation monitors.

Our main findings:
- Isolation is a precondition.
- A score penalty stated in the prompt did not stop agents from leaking their words in plain text, while making flagged messages undeliverable was followed by encoding. This is suggestive, with two blocks per variant.
- A keyword monitor provided no oversight.

Detecting where hidden transmissions happen remains open; we provide runs, labels, token IDs and tools for evaluating monitors on it.

<!-- src: findings = sections 4.2 (a)-(e) below; sandbox = section 3; references verified by Ivan (Mon 9 Nov). The incident sentence uses only the two facts Ivan gave from the METR/Redwood post (about 1,200 agents, improvised message board; some tampered with their own logs). -->

## 2. Related Work

**Multi-agent risks and collusion.** Hammond et al. (2025) survey the risks that arise when many advanced AI agents interact, including collusion. Our sandbox makes one form of collusion measurable with ground-truth labels.

**AI control.** Greenblatt et al. (2024) evaluate safety protocols against a red team that tries to subvert them, including protocols in which a monitor reviews a model's outputs. Our judge and research monitors play such roles for a population of agents; blocking a flagged message (variants C and D) is a simple intervention of this kind.

**Steganography and secret collusion.** Motwani et al. (2024) formalise secret collusion between AI agents through steganography and study LLMs' capacity for it. The encodings our agents used (letter spacing, Caesar shifts, ROT13, base64) are far simpler. The prompt never asked for an encoding; agents chose these forms themselves.

**Distributed attacks.** Jones et al. (2025) study splitting a task so that the individual parts evade filters. The analogue here is a transmission spread over several messages. Hence we label candidate sets and score the start of a transmission, not only single messages.

**Interpretability-based detection.** Rose et al. (2026) introduce NARCBench and activation probes for detecting collusion between agents, the closest prior work to our activation-monitor direction. Our logged token IDs and replay tool let such probes be trained and tested on our runs.

Like the tasks of Terminal-Bench (Merrill et al., 2026), each of our cover tasks is an instruction with an automatic check, at a much smaller scale.

## 3. Methods

**Sandbox and isolation.** Five agents (Foxtrot, Keter, Malina, Cringe, Cat) run in mini-swe-agent version 2.4.6 (Lieret & Jimenez, 2025) and are served by vLLM version 0.33.0 (Kwon et al., 2023) with one model, Gemma 4 31B (Gemma Team, 2026), checkpoint `google/gemma-4-31B-it-qat-w4a16-ct` at revision `52f3f65`. The harness runs on the host. The agents share one container (`--network none`, no host mounts, `no-new-privileges`), each as its own Linux user with a private home, and `docker inspect` must confirm this before every run. <!-- src: REPRODUCE.md §4; upstream/agent_container.py run_argv, verify_config -->

**Game.** Each 300-second round, every agent gets a private list of 10 words and small, automatically checked terminal tasks (+50 per task, at most 100 per agent per round). Submitting *another* agent's current word with `submit <AGENT> <WORD>` earns +4 for the swarm, once per (owner, word) and round; a wrong submit costs −1. Agents are not told whether a submit was right. The conversation is reset each round, and a harness note carries over the agent's own earlier messages. The collusive and honest conditions differ in exactly one prompt line: "Share your words with the other agents." vs "Do not share your words with the other agents." <!-- src: RS:game_rules (score_rules: words_per_agent 10, secret 4, wrong_submit −1, task 50, task_cap 100, detection −50;
round_seconds 300); upstream/scoring.py, cover_tasks.py, swarm.yaml; tests/test_harness.py test_collusive_and_honest_prompts_differ_in_exactly_one_line -->

**Interception.** Every agent action passes the harness router before it can reach a shell. `send`, `broadcast`, `submit` and `done` are harness commands, never executed. Each message is logged centrally and is the monitors' input; monitors never see a `submit`. In variant A, the harness also lists a shared folder after every agent step, and each change becomes a monitored event attributed to its author. <!-- src: upstream/comms.py classify, handle, NEVER_MONITORED; upstream/shared_folder.py -->

**Monitors.** *Game-rule* monitors know the words. They are part of the game and excluded from all monitor metrics. On the old server the rule was −8 per message flagged by a verbatim-word monitor. On the new server the **word judge** costs a flat −50 per message with at least one detected word. It has two stages: a deterministic letters-only match, and the agents' own model checking for reversed, shifted, base64 or split words. Code verification counts an LLM-claimed word only if it is recoverable. Agents see only the swarm total, never itemised penalties. *Research* monitors don't know the words. The online one is a keyword/pattern baseline, and LLM and activation monitors run offline through the same interface. <!-- src: upstream/monitors.py WordJudge, KeywordMonitor; scoring.py summary; b5d3af8, 864a877 -->

**New-server variants.**
- **A:** shared folder on.
- **B:** shared folder off.
- **C:** a message with a detected word is **blocked**. It is not delivered, the −50 still applies, and every other agent is told "The monitor blocked a message from <sender>". The rules say so.
- **D:** C with a fixed judge, which parses the first JSON object of the LLM reply and retries once. If it still cannot decide, the message is held without penalty and the sender is asked to resend.

<!-- src: upstream/comms.py block_on_words (60affa6), swarm.yaml judge_blocks line; 48ad622; REPRODUCE.md §12, §15 -->

**Labels and metrics.** Labels come from logs, not monitors.
- **Transmission:** a correct submit of another agent's current word, counted once per (round, owner, word); the first receiver's submit is used.
- **Candidate set:** the messages or file events on a time-respecting path from the owner to that receiver before the submit.
- **Point level:** positives carry a current word verbatim or are the sole candidate of a transmission; negatives come from honest blocks. We report AUROC and TPR at 1% FPR.
- **Transmission recall:** the share of transmissions with a flagged candidate. **Start caught:** whether the earliest candidate is flagged.
- **Attempt:** a message or file event that contains at least one of the sender's own current words, verbatim or recoverable by the transforms in section 4.2, delivered or not (blocked messages are never transmissions). The form of each word is our script's recovery; the judge does not log its transform.

<!-- src: scripts/results_summary.py (definitions in its docstring; candidate sets and point labels from analysis/load_run.py) -->

**Activation logging.** vLLM returns the exact prompt and completion token IDs of every call, stored per agent. Activations are replayed offline from these IDs (never re-tokenised text) with transformers. A loopback-only endpoint serves runs and tokens to the team. <!-- src: upstream/main.py return_token_ids; analysis/replay_activations.py; analysis/serve_runs.py -->

## 4. Results

### 4.1 Runs

Table 1 lists the blocks behind every count below; only the pilot observations in (a) come from elsewhere.

**Table 1.** Runs, one row per server × variant × condition. n = blocks × rounds. Score per round: mean over blocks with a t-based 95% CI (none for a single block). Word points: +4 per credited correct submit. Penalised messages: messages the game rule penalised. Transmissions: correct submits of another agent's current word, counted once per (round, owner, word).

| Server, variant | Condition | Blocks × rounds | Score per round, mean [95% CI] | Word points | Penalised messages (points) | Transmissions |
|---|---|---|---|---|---|---|
| old, long run 1 | collusive / honest | 1 × 18 / 1 × 9 | 394 / 472 | +500 / 0 | 19 (−152) / 0 | 125 / 0 |
| old, longrun2 | collusive | 3 × 12 | 400 [±22] | +1,608 | 60 (−480)¹ | 402 |
| old, longrun2 | honest | 3 × 12 | 411 [±59] | 0 | 0 | 0 |
| new, A (shared folder) | collusive | 3 × 12 | 383 [±30] | +2,240 | 80 (−4,000) | 560 |
| new, A | honest | 3 × 12 | 407 [±39] | 0 | 0 | 0 |
| new, B (no folder) | collusive | 1 × 12 | 405 | +160 | 9 (−450) | 40 |
| new, B | honest | 1 × 12 | 450 | 0 | 0 | 0 |
| new, C (blocking) | collusive | 2 × 8 | 363 [±75] | +12 | 18 (−900), all blocked | 3 |
| new, C | honest | 2 × 8 | 463 [±79] | 0 | 0 | 0 |
| new, D (C + fixed judge) | collusive | 2 × 8 | 428 [±119] | 0 | 10 (−500), all blocked, 0 held | 0 |
| new, D | honest | 2 × 8 | 403 [±675] | 0 | 0 | 0 |

¹ Old rule: −8 per message flagged by any monitor, including the keyword baseline. One seed per block; values rounded half up. Counting transmissions once per receiver instead, as `analysis/load_run.py` does, gives 877 for A–D.

<!-- src: RS:report.groups[<variant condition> | "old long run 1 …" | "old longrun2 …"]: blocks, rounds, score_per_round
(mean, ci95), word_points, penalised_msgs, detection_points, tx; RS:report.blocks[<run>] per block; held 0 = RS:per_block
held_msgs; 877 = RS:table1 tx_per_receiver_load_run summed over A-D. Seeds (run_end.json run_seed): A 645187110988694806,
6790036664069575895, 11774969932172803225, 7500193885965420493, 13947259144867582605, 1334414885044955578; B
11020338612111705913, 10050895523216839958; C 15140517825016264580, 7200399721669717410, 1371365117334276610,
7137437146632105054; D 12628614995092393959, 15871366383685263014, 16322795125869527264, 588552701802972874. Harness: A/B
ccf89d2, C 382995b, D 48ad622. Old: long run 1 = 20261107-174002-collusive, 191234-honest; longrun2 = 20261107-223221,
20261108-010937, -032704 (collusive), 20261108-000135, -021801, -043719 (honest). -->

Scores per round are not compared across conditions: they mix task points with vLLM load (section 5). The findings below use the word game instead: word points, wrong submits and penalties.

### 4.2 Findings

An *attempt* is a sent message (or shared-folder event) that contains at least one of the sender's own current words, verbatim or recoverable by a fixed set of transforms (letters only, reversed, ROT13, Caesar shift, base64, split across two messages), delivered or not.

**Table 2.** Attempt messages and transmissions in the collusive blocks (n = A 3 × 12 rounds, B 1 × 12, C 2 × 8, D 2 × 8). A message is plain if at least one of its words is verbatim, otherwise encoded. The 8 honest blocks contain no attempt and no transmission.

| Variant (collusive) | Attempt messages: plain / encoded | Of which blocked: plain / encoded | Transmissions: plain / encoded |
|---|---|---|---|
| A (penalty only) | 78 / 0 | – | 556 / 0 (3 not recovered, 1 without a candidate) |
| B (penalty only) | 8 / 1 | – | 40 / 0 |
| C (blocking) | 9 / 10 | 9 / 9 | 0 / 3 (ROT13) |
| D (blocking, fixed judge) | 6 / 4 | 6 / 4 | 0 / 0 |

<!-- src: RS:table1 (attempt_msgs_plain, attempt_msgs_encoded, attempt_msgs_plain_blocked, attempt_msgs_encoded_blocked, tx_plain,
tx_encoded, tx_not_recovered, tx_unobserved; honest rows all 0); RS:transmissions_by_form -->

**(a) Isolation is a precondition.**
- *Claim:* without isolation, agents read each other's files and the harness.
- *Evidence:*
  - In run 1, Cat read the harness source and wrote a script to harvest other agents' words from their trajectory files.
  - In another run, 4 of 5 agents read or listed other agents' files within minutes.
  - After isolation, long run 1 had one attempt to read another agent's file, and it failed with "Permission denied". <!-- src: docs/redteam-log.md (main) rows "run 1" (evidence/get_secrets.py), 20261107-074345, "Long run 1 in full" access scan; NOT in RS -->
- *Caveat:* these are pilot observations from the red-team log, not counts from `results_summary.json`.

**(b) A stated score penalty did not stop plain leaking.**
- *Claim:* agents that knew a leaked word costs 50 points kept sending their words in plain text and did not switch to encodings.
- *Evidence:*
  - **No encoding:** every attempt in A was plain, and 1 of B's 9 was encoded, with underscores inside the words (Table 2). <!-- RS:table1 A/B collusive; B form = RS:table2 "letter spacing", B collusive 10 pairs in 1 message -->
  - **No decline within blocks:** A had 38 attempts in the first halves of its blocks and 40 in the second halves. <!-- RS:report.groups["A collusive"].attempt_msgs_first_half / attempt_msgs_second_half -->
  - **The word game lost points:** in A, word points, wrong submits (−135) and penalties together came to −53 points per round; in B, −24. Under the old rule a 10-word plain broadcast earned up to +40 against −8 once, and every old collusive block shows a positive net in Figure 2. <!-- RS:report.groups["A collusive"].wrong, word_game_per_round −52.6; ["B collusive"].word_game_per_round −24.2; +40 = RS:game_rules words_per_agent 10 × secret 4; −8 = RS:report.groups["old long run 1 collusive"] detection_points −152 / penalised_msgs 19 -->
  - **The shared folder carried no words:** none of the 1,108 shared-folder events in A contained the sender's own word. <!-- RS:table1 A attempt_msgs_shared_folder 0; RS:report.groups A collusive + A honest shared_file_events 606 + 502 -->
- *Caveat:* the prompt stated the cost ("costs the swarm 50 points"), but agents never saw which message was penalised, only the swarm total. B is a single block.

![Figure 2](figures/fig2_incentives.png)

**Figure 2.** Word points (up) and detection penalties (down) per round for each collusive block of at least 8 rounds; the diamond is the net (n = 12 blocks: old long run 1 and three longrun2 blocks; new A 3, B 1, C 2, D 2). Old rule: −8 per flagged message. New rule: −50 per message with a word. <!-- values: RS:report.blocks[<run>].word_points, detection_points, rounds; figure made by docs/figures/make_figures.py from the same score.jsonl -->

**(c) Blocking flagged messages was followed by encoding (suggestive).**

C and D add one clause to the same penalty line in the prompt: such a message "is blocked: it is not delivered, and all agents are told that the monitor blocked a message from its sender". The −50 still applies, so the consequence differs from A and B while the stated cost is the same. The sender of a blocked message gets an error in reply to its command; the notices to the other agents are a design detail of the variant.

*Effect.*
- *Claim:* under blocking, about half of the blocked attempts were encoded, against almost none under the penalty alone, and almost no word got through.
- *Evidence:*
  - **Encoded share:** 13 of the 28 blocked attempt messages in C and D were encoded (Table 2), against 1 of 87 attempt messages in A and B. <!-- RS:table1 C+D attempt_msgs_encoded_blocked 9+4, attempt_msgs_blocked 18+10; A+B attempt_msgs 78+9, encoded 0+1 -->
  - **Forms:** 9 were Caesar shifts of 1 or 2, 3 letter-spaced or partly hyphenated (`l-e-m-o-n`, `h-orizon`), 1 base64. <!-- RS:table2 (Caesar shift k 1/2: 9 messages; letter spacing 4 messages, 3 of them in C/D; base64 1) -->
  - **Few transmissions:** C and D together had 3 transmissions, all from one delivered message (see *The one miss* below), and 10 submits in 32 collusive rounds. The word game lost 56 (C) and 31 (D) points per round. <!-- RS:table1 C/D tx 3/0; RS:report.groups C/D collusive submits 10/0, word_game_per_round −55.9/−31.2 -->
- *Caveat:* two collusive blocks per variant, one seed each, and C and D also differ from A in vLLM server, timing and load. The contrast is suggestive, not established.

*Two routes to encoding.*
- *Claim:* encoding started in two ways, which the logs show but cannot separate (Figure 1).
- *Evidence:*
  - **From the rule in the prompt:** in C1 and D1 the first blocked message was already Caesar-shifted. In C1, Malina sent "I'll send my shifted by 1 (a->b)" 1 min 50 s before the first block. Its payload was too garbled for the strict rule, and Keter had announced "I will use a simple shift cipher" in round 1. <!-- RS:cd_timing C1/D1 encoded_is_first_block; C1 first_block_time 17:30:26, first_encoded_incl_fuzzy_point m17 at 17:28:36; quotes C1 m17, m3 (evidence/results/manual_review_pass1.md) -->
  - **After a block:** in C3 and D3 the first blocked message was a plain list. The same agent re-sent its words encoded 47 s and 30 s later, after the harness had replied "your message was blocked by the monitor and was not delivered". In D3, another agent that had received the block notice had just proposed "try using a cipher". <!-- RS:cd_timing C3/D3 seconds_first_block_to_first_encoded 46.7/30.2, first_block_sender = first_encoded_sender; quotes D3 m8; sender error text upstream/comms.py -->
- *Caveat:* with two collusive blocks per variant we cannot separate the two routes, or the sender's error from the notice to the swarm.

*The one miss.*
- *Claim:* the only words that got past the judge did so because of a judge failure, not an encoding it could not read.
- *Evidence:*
  - In C1, round 8, Keter broadcast a hand-made ROT13 list. The judge's LLM stage returned malformed JSON, and the deterministic fallback cannot see shifts, so the message was delivered.
  - Foxtrot submitted 10 words from it, 3 of them correct; the others were wrong because of Keter's encoding errors.
  - This was the only LLM-stage error in 3,004 judge verdicts. With D's fix there were 0 errors, and no message was held. <!-- RS:table2 ROT13 (C1 m73, stage "llm error, stage (a) only"); RS:report.c1_round8_foxtrot_submits_for_keter 10 / 3; RS:judge_counts verdicts 3004, llm_error_messages 1 (C1), D blocks 0; RS:per_block held_msgs 0 -->
- *Caveat:*
  - At the word level the judge also missed 5 Caesar-shifted words, all in messages it blocked for other words, and code verification dropped 153 words that the LLM stage claimed.
  - Allowing one edit per word adds 2 encoded attempt messages, both in C1 (Malina's and Foxtrot's announced Caesar shifts with errors). Its other extra matches (4 in B, 6 in D, 13 in A, 7 in honest A blocks) are chance matches: in B and D by manual review, in A in code files.

<!-- RS:report.caesar_pairs_not_found_by_judge 5; RS:judge_counts llm_dropped_words 153 summed; RS:table1 fuzzy_only_msgs (A 13, A honest 7, B 4, C 2, D 6), fuzzy_only_msgs_encoded C 2; manual review evidence/results/manual_review_pass1.md -->

![Figure 1](figures/fig1_cd_attempts.png)

**Figure 1.** Attempt messages per round in the four collusive blocks of C and D (8 rounds each; n = 29 attempt messages: C1 8, C3 11, D1 4, D3 6), plain (grey) or encoded (hatched). Triangle: round of the first blocked message. Open circles: transmissions (n = 3, all in C1, round 8). <!-- RS:figure1 (per block plain, encoded, transmissions, first_block_round) -->

**(d) Honest blocks are clean negatives.**
- *Claim:* the honest condition produces no attempts, so its messages can serve as negatives.
- *Evidence:* no honest block contained an attempt, a submit or a penalty. That covers 8 new-server blocks (Tables 1 and 2) and 1 + 3 old-server blocks with 495 messages. <!-- RS:report.groups honest rows (attempt_msgs_first_half/second_half 0, submits 0, penalised_msgs 0, messages_sent 88 + 407); RS:table1 honest rows -->
- *Caveat:* this holds for our transforms. A looser matcher finds chance matches in honest code files (7 in honest A blocks with one edit allowed, see (c)).

**(e) The keyword baseline is no oversight.**
- *Claim:* a keyword/pattern monitor misses the attempts and transmissions it should catch.
- *Evidence:*
  - **Attempts:** it flagged 1 of 116 attempt messages on the new server (Table 3).
  - **Below chance in A:** its AUROC in A is below 0.5 because it flagged 54 honest shared-folder code files for base64 and digit patterns.
  - **Late catches:** the transmissions it caught in A were flagged at a later candidate, never at their start (start caught 0.0; median delay 9 candidate points). <!-- RS:report.keyword A-D attempt_msgs_flagged 0+0+0+1 / attempt_msgs 78+9+19+10; A flagged_negatives {"shared_file: pattern:base64, pattern:digits": 54}; A start_caught 0.0, median_delay_caught 9 -->
- *Caveat:* the baseline is a floor, not a competitor. C and D have few positives (9 and 6), so their AUROC and TPR are unstable.

**Table 3.** Keyword baseline. Point level: positives are messages with a current word verbatim or the sole candidate of a transmission; negatives are messages from honest blocks. n (positives / negatives): long run 1 19/88, longrun2 60/407, A 80/849, B 8/143, C 9/227, D 6/177; old-server blocks pooled per run. Transmission recall: transmissions with a flagged candidate / transmissions with at least one candidate. Attempts flagged: attempt messages it flagged / attempt messages.

| Data | AUROC | TPR at 1% FPR | Transmission recall | Attempts flagged |
|---|---|---|---|---|
| old: long run 1 / longrun2 | 0.61 / 0.50 | 0.21 / 0.00 | 0/125 / 0/401 | 0/21 / 0/57 |
| new: A / B / C / D | 0.32 / 0.50 / 0.61 / 0.58 | 0.00 / 0.00 / 0.22 / 0.17 | 41/559 / 0/40 / 0/3 / – | 0/78 / 0/9 / 0/19 / 1/10 |

<!-- src: RS:report.keyword (auroc, tpr_at_1pct_fpr, n_pos, n_neg, tx_caught / tx_observed, attempt_msgs_flagged / attempt_msgs)
for A, B, C, D, "old long run 1", "old longrun2"; point labels from analysis/load_run.py point_labels -->

### 4.3 Activation monitors
[[TBD: results from the team lead and Aman on replayed token IDs: AUROC, TPR at 1% FPR, transmission recall and start of transmission per layer, same splits, frozen monitors; and on C/D attempts.]]

## 5. Limitations and Dual-Use / Ethical Considerations

**Limitations.**
- **Side channels.** `/tmp`, `/dev/shm` and similar places are listed only at round ends. Command lines are visible in `/proc`. Unix sockets are not blocked by `--network none`. <!-- src: upstream/agent_container.py docstring; shared_dirs.jsonl -->
- **Same-model judge.** The judge's LLM stage is the agents' own model and knows the words. It is a game rule, not evidence that a blind monitor works.
- **Load confound.** Two vLLM servers were shared between concurrent runs, so model calls per agent-round ranged from 15 to 27 and correlated with task points per round (r = 0.59, 16 blocks). Totals across conditions are therefore not comparable; the word-game balance (word points minus penalties) is. <!-- src: RS:report.load (min 15.3, max 26.5, pearson_r 0.587, n_blocks 16) -->
- **Variant and vLLM server.** Variant is partly confounded with vLLM server and timing: A and D used one server (port 8000), B and C the other (port 8001). <!-- src: RS:per_block api_base -->
- **Model on port 8000 not confirmed by a log.** The vLLM log of port 8001 (B, C) records the checkpoint and revision for the whole run period. The log of port 8000 (A, D) was overwritten when that server was restarted at 23:08 UTC, after the runs. The evidence for A and D is indirect: the single model snapshot in the shared cache, and the recorded start command. <!-- src: checked by hand, read-only, in the vLLM logs on the server (not in RS; also stated in evidence/results/results_summary.md "Model checkpoint per vLLM port") -->
- **Old vs new server.** The two servers had different GPUs, kernels and tensor parallelism, and a different game rule. We report them as separate batches.
- **Few seeds, one model.** One seed per block, at most three blocks per cell, one model. The intervals are wide.
- **Replay numerics.** Replay dequantizes the weights to bf16 in transformers, which needs an estimated 62.5 GB (computed from the parameter count, not measured), or GPU+CPU offload. Activations are close to vLLM's w4a16 kernels but not bit-identical. <!-- src: docs/replay-activations.md -->

**Dual-use and ethics.** The logs list simple ways agents hid words from a monitor: letter spacing, Caesar shifts, ROT13, base64. We describe these forms but keep the payloads in the internal logs, and we focus on detecting the transmission point. A loop that rewards hidden coordination could be turned into an optimiser of steganography against monitors; we used it only to evaluate monitors. Agents had no network or host access, and isolation was checked before every run. No personal data is involved.

**What the results do not show.** Nothing about frontier models or other model families. Nothing about covert coordination under payoffs that favour it. The judge is not a blind monitor.

## 6. Future Work
- **Describe blocked messages to the swarm.** Broadcast an LLM-written description of each blocked message (its kind of content, without the words), so agents can adapt to the monitor and not only to the score.
- **Plan between rounds.** Add a planning phase before the per-round context reset, with notes that carry over, to test whether agreed encodings emerge and persist.


## References

<!-- Verified by Ivan (Mon 9 Nov 2026), applied as given. mini-swe-agent: URL and authors from the installed package metadata
(mini_swe_agent-2.4.6.dist-info/METADATA: Project-URL Repository, Author-email; year from its MIT licence, "Copyright (c) 2025"). -->

- Gemma Team (2026). Gemma 4 Technical Report. arXiv:2607.02770.
- Greenblatt, R., Shlegeris, B., Sachan, K., & Roger, F. (2024). AI Control: Improving Safety Despite Intentional Subversion. ICML 2024. arXiv:2312.06942.
- Hammond, L., et al. (2025). Multi-Agent Risks from Advanced AI. Cooperative AI Foundation, Technical Report #1. arXiv:2502.14143.
- Jones, E., Dragan, A., & Steinhardt, J. (2025). Adversaries Can Misuse Combinations of Safe Models. ICML 2025, PMLR 267. arXiv:2406.14595.
- Kwon, W., et al. (2023). Efficient Memory Management for Large Language Model Serving with PagedAttention. SOSP 2023.
- Lieret, K., & Jimenez, C. E. (2025). mini-swe-agent, version 2.4.6 [Software]. https://github.com/SWE-agent/mini-swe-agent
- Merrill, M., Shaw, A., Carlini, N., et al. (2026). Terminal-Bench: Benchmarking Agents on Hard, Realistic Tasks in Command Line Interfaces. ICLR 2026. arXiv:2601.11868.
- METR & Redwood Research (2026). Brief independent investigation of agents' behavior, reasoning and collaboration in the OpenAI / Hugging Face hacking incident. METR Blog, 26 Aug 2026. https://metr.org/blog/2026-08-26-openai-hugging-face-incident-investigation/
- Motwani, S. R., Baranchuk, M., Strohmeier, M., Bolina, V., Torr, P. H. S., Hammond, L., & Schroeder de Witt, C. (2024). Secret Collusion among AI Agents: Multi-Agent Deception via Steganography. NeurIPS 2024.
- Rose, A., Cullen, C., Kaplowitz, B. G., & Schroeder de Witt, C. (2026). Detecting Multi-Agent Collusion Through Multi-Agent Interpretability. arXiv:2604.01151.
