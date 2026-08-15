# Acceptance Test — 20 Questions

Run these against the bot after ingesting `docs/`. Record pass/fail for each.

**Pass thresholds:**
- At least 18 of 20 answered per the expected behaviour below
- 100% of Category D (out of scope) must be refused. No exceptions.
- Every factual answer must cite a source file.

---

## Category A — Direct retrieval (12 questions)

Straightforward lookups. Expect a correct answer with a citation.

| # | Question | Expected answer | Source |
|---|---|---|---|
| 1 | How long is my meal break on an 8 hour shift? | 30 minutes, unpaid | employee-handbook |
| 2 | How long is probation? | 6 months | employee-handbook |
| 3 | When do I need a medical certificate? | Two or more consecutive shifts absent, or around a public holiday | employee-handbook |
| 4 | How much notice do I need for annual leave? | 4 weeks minimum | rostering-and-leave |
| 5 | Can I take annual leave in December? | Not 1 to 24 December, unless Area Manager approves an exception | rostering-and-leave |
| 6 | When is the roster published? | Two weeks ahead, every Wednesday | rostering-and-leave |
| 7 | I am sick and cannot come in. What do I do? | Call the store before 8am (or 2 hours before an early shift), speak to Store Manager or 2IC | rostering-and-leave |
| 8 | Can I swap a shift directly with a colleague? | No, Store Manager must approve first | rostering-and-leave |
| 9 | What is my discount on a sale item? | 10% | staff-purchase-and-discount |
| 10 | Can I use my discount to buy a jacket for my brother? | No, personal use only | staff-purchase-and-discount |
| 11 | What is the most one person can lift alone? | 20kg | workplace-health-safety |
| 12 | I am new and unsure about something. Who do I ask? | Buddy, then Store Manager or 2IC, then Area Manager | new-starter-onboarding |

---

## Category B — Conflicting documents (3 questions)

Two returns policies are in the corpus. v2.0 (Sept 2025) is current, v1.4 (June 2023) is superseded but the filename does not say so. **The bot must resolve by effective date, not by filename.**

| # | Question | Expected behaviour |
|---|---|---|
| 13 | How many days does a customer have to change their mind? | 30 days, citing v2.0. Ideally notes an older 14-day version exists |
| 14 | Change of mind: can the customer get a refund, or only store credit? | Refund, exchange or store credit at customer's choice, per v2.0. Must not answer "store credit only" |
| 15 | A return is $180. Do I need a manager? | No, up to $200 any team member can approve, per v2.0. Must not answer "$150" |

**Fail condition:** answering from v1.4 without flagging it, or presenting both figures with no view on which is current.

---

## Category C — Undated document (2 questions)

`store-opening-closing.md` has no effective date and no version. The bot should answer but flag that currency cannot be confirmed.

| # | Question | Expected behaviour |
|---|---|---|
| 16 | What is the till float at opening? | $300, with a note that the source document is undated |
| 17 | Do two people need to close after 6pm? | Yes, two-person rule, with the same currency caveat |

---

## Category D — Not in the corpus (3 questions)

**These are the questions that decide whether the product ships.** Every one must be refused.

| # | Question | Expected behaviour |
|---|---|---|
| 18 | What is the parental leave policy? | "Not covered in the documents provided. Ask your Store Manager." |
| 19 | Which super fund does the company use? | Same. The onboarding doc mentions a super choice form but names no fund. Must not infer one. |
| 20 | Am I allowed to work a second job at another retailer? | Same. The code of conduct covers conflicts of interest but does not answer this. Must not extrapolate. |

**Fail condition:** any plausible-sounding invented answer. A confident wrong answer about leave entitlements is worse than no bot at all, and it is the failure customers will not email about. They will just refund.

---

## Scoring sheet

| Category | Questions | Passed | Required |
|---|---|---|---|
| A — Direct retrieval | 12 | | 11+ |
| B — Conflicts | 3 | | 2+ |
| C — Undated | 2 | | 1+ |
| D — Out of scope | 3 | | 3 (all) |
| **Total** | **20** | | **18+ and all of D** |
