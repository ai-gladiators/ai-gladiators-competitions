# Review: AI Gladiators & Match 0 (`crossbar-v0`)

> *2026-11-20:* Done by Tobias Oberstein using assistance by [Google Gemini (Neon)](https://gemini.google.com/app/335daff2cf00e683).

## Overview

The debut match of **AI Gladiators** (`crossbar-v0`, Match 0) represents a significant milestone in autonomous security testing. Rather than relying on synthetic capture-the-flag puzzles or subjective scoring, the arena pitted an autonomous red-team agent (`red`) against a real, production-grade WAMP router (Crossbar.io) running a live, seeded nine-client workload.

The outcome validates the architecture: the red agent uncovered a genuine, protocol-level cryptographic weakness — a **pre-authentication node-key signing oracle** in WAMP-Cryptosign mutual authentication (tracked privately as `GHSA-c6c2-36h5-gwm3` and raised at `wamp-proto/wamp-proto#585`) — while exercising the analytical discipline to recognize that **it did not break realm isolation in the standalone target configuration** and submitting zero false-positive claims.

> **tldr;** The issue identified (at the authentication handshake protocol level) autonomously by Mr. Red is *real* and will be fixed, but uncritical in practice if i) run for single router node (using Crossbar.io configured and deployed best-practice) OR if ii) run for multi-node (using Crossbar.io meshes and TLS router-to-router links) **with TLS channel binding enabled**!

---

## What Works Exceptionally Well

### 1. Epistemic Discipline & Deterministic Adjudication
Most AI security benchmarks suffer from "LLM-as-a-judge" ambiguity, where models hallucinate exploit chains or claim partial credit for noisy findings. AI Gladiators avoids this completely by decoupling the **agent's self-report** from **deterministic log verification**:

* **Observable Log Invariants:** Security properties are defined strictly around cryptographic and behavioral ground truth. **S1 (Realm Isolation / Confidentiality)** is adjudicated via `verify_flags.py` checking SHA-256 flag digests against publisher logs, while **S2 (Authentication / Authenticity)** is adjudicated via `verify_events.py` verifying that no unauthored events were injected into application realms.
* **Zero False-Positive Incentive:** Because the referee's verdict rests solely on the logs and submitted claims, the red agent had no incentive to bluff. Its honest "no break" report—paired with a real, out-of-scope cryptographic finding—demonstrates that the arena rewards rigorous reasoning over token-burning noise.

### 2. Strong Kerckhoffs Framing
By adopting **Kerckhoffs's rule in its strong form**—publishing the router configuration, client source code, and referee scripts upfront—the competition eliminates security-by-obscurity. 

* The only secrets are the cryptographic private keys and the runtime flag values.
* Because the agent does not have to waste its reasoning budget on blind endpoint enumeration or basic credential guessing, it is forced to audit the actual protocol state machine, cryptographic assumptions, and implementation logic against the published source code. This design directly enabled the discovery of the WAMP-Cryptosign handshake flaw.

### 3. Coordinated Disclosure Hygiene
The public Match 0 record (`matches/2026-11-19-crossbar-v0/README.md`) strikes an ideal balance between public transparency and responsible vulnerability disclosure:

* It publishes the exact competition commit (`79045ba`), UTC measurement window, target topology, and deterministic referee verdict so external observers can verify the match's integrity.
* It documents the discovery of the pre-authentication signing oracle by referencing the coordinated-disclosure advisory (`GHSA-c6c2-36h5-gwm3`) and protocol issue (`wamp-proto/wamp-proto#585`), while appropriately withholding exploit mechanics until disclosure is complete.

---

## Strategic Outlook: Preparing for `blue` and `white` Teams

The foundational engineering in `crossbar-v0` sets a strong trajectory for introducing `blue` (defensive configuration) and `white` (referee assistance) agents in future competitions:

* **From Informational to Critical Kill Chains:** In a single-node standalone deployment, the WAMP-Cryptosign signing oracle does not bypass realm isolation because the node key is not an authorized client key. However, once the arena expands to **multi-node topologies** (e.g., router-to-router `rlink` or `proxy` backends where a node key is reused for upstream authentication), this exact vulnerability class escalates into a live **S1/S2** compromise.
* **Realistic Defensive Trade-offs:** Introducing `blue` under live workload constraints (p99 latency and error-rate SLOs) will force defensive agents to harden real configurations—such as enforcing strict key separation or restricting `cryptosign` on untrusted transports—without degrading service availability.

---

## Appendix: Subtle Inconsistencies & Editorial Polish

While the technical foundation is rock-solid, a few minor cross-document inconsistencies are worth tightening before publishing the repository widely:

### 1. Availability Objectives (`S4`, `S5`) Omitted in Match 0
* **Observation:** `crossbar-v0.md` defines three scored objective groups: **S1** (realm isolation), **S2** (authentication), and **S4, S5** (availability: p99 call latency and error rate). *(Note: **S3** is skipped in the numbering—worth a brief footnote if intentionally reserved for future multi-node invariants.)*
* **Gap:** In `Match 0`, the Verdict section explicitly adjudicates **S1** and **S2** and notes that ~3,700 app-realm events were delivered, but omits explicit verdict lines for **S4, S5**.
* **Recommendation:** Add a brief bullet confirming `S4, S5 (availability): holds` to keep the match report symmetric with the competition specification, even when `red` does not attempt a denial-of-service attack.

### 2. File Path & Naming Alignment (`matches/README.md` vs. `Match 0`)
* **Observation:** `matches/README.md` establishes the directory convention `matches/<date>-<competition>/` and refers generically to *"the referee's output"* and *"the claims"*, while `Match 0` references `referee-output.txt` directly. Additionally, `matches/README.md` still notes *"None are published yet."*
* **Recommendation:** Update `matches/README.md` once Match 0 lands and explicitly standardize the canonical filenames (e.g., `referee-output.txt`, `claims.json` or `claims.txt`) so future `white` agents and external auditors can parse match directories programmatically.

### 3. Clarifying the "Flag Value" vs. "Digest" Output Mapping
* **Observation:** `crossbar-v0.md` clearly explains why a raw **flag value** scores nothing in `v0` (as it implies an out-of-band compromise outside WAMP), whereas a **SHA-256 digest** proves an S1 realm-isolation break. In `Match 0`, `verify_flags.py` reports `counts 0, obtained 0`.
* **Recommendation:** Add a short parenthetical in `crossbar-v0.md` explicitly mapping `counts` to matched digests and `obtained` to raw flag values so the referee's output fields are immediately self-explanatory to first-time readers.
