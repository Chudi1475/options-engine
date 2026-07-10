# Edge lessons — confirmed only

Rules that survived the anti-overfit gate (tuned on the first half of days
only; the untouched second half had to hold >= 60% win rate on >= 60 trades
with positive avg R) or structural facts proven by the data. Nothing here is
in-sample-only folklore; rejected candidates are listed so they do not get
re-tried blind.

## Round 3 (backtest_chart_v3.py, reports/chart_backtest_round3.json, 2026-07-08)

Baseline entering the round: v2 winner at 63.9% OOS inside the v3 engine
(233 trades, avg R 0.083). Round result: 75.2% OOS on 125 trades, avg R
0.133 (+16.6R). 77 days, 8 symbols, split 2026-06-01, 7 total OOS looks.

### Confirmed

1. FVG GAP SIZE IS THE DOMINANT QUALITY SIGNAL. IS gradient under the v2
   winner: gap < 0.5 ATR -> 57.9%, 0.5-1.0 -> 60.2%, >= 1.0 ATR -> 78.5%
   (51/65). Raising the min gap from 0.3 to 1.0 ATR is most of the round's
   entire gain, and it held on the untouched half (75.2% OOS). Equivalent
   framing: because risk = gap/2 + 0.1 ATR, stops tighter than ~0.4 ATR
   (gap < 0.6) ran 50% IS — tight-stop trades are the loss factory.
2. TIGHTER TARGET BEATS 0.7R. TP 0.5R all-out won selection and held OOS with
   BOTH higher win rate and higher avg R (0.133 vs 0.083) than 0.7R. At 75%
   wr, 0.5R all-out clears its 66.7% breakeven with margin.
3. SYMBOL EDGE IS UNEVEN AND THE DROPS HELD. GBP/USD (48.1% IS) and SPX
   (58.6% IS) drops were selected in-sample and the config held OOS. The SPY
   drop is marginal: the runner-up keeps SPY and did slightly better OOS
   (76.0% on 129 trades, avg R 0.144) — treat SPY as neutral, GBP/USD and
   SPX as confirmed drops.
4. LOSS ANATOMY: THE REMAINING LOSSES ARE MOSTLY BAR-GRANULARITY TIES, NOT
   BAD LOCATIONS. Of 65 IS losses in the v2-winner replay, 47 (72%) had
   already reached >= 90% of the 0.7R target (MFE) on a bar that also spanned
   the stop, and score as losses only under the conservative tie=loss rule;
   just 5 never went favorable at all. Pushing win rate further by filtering
   entry quality has a ceiling — the next real gain is execution-side
   (finer-than-5m fills would resolve the tie ambiguity honestly).
5. The v2 lessons still stand: grade A FVG only, CE limit entry (TTL 1h),
   stop just beyond the FVG far edge +0.1 ATR, skip when run-from-open
   >= 1.0 ATR, max 2 trades/symbol/day.

### Rejected candidates (tested, did NOT earn a place)

- Liquidity-sweep-required: IS said no (64.8% with sweep vs 71.9% without),
  and the sweep-required config that first held the gate did WORSE OOS than
  its no-sweep sibling (72.1% vs 78.2%). A raid before the FVG adds nothing
  once the gap is big.
- CE in discount/premium: real solo IS signal (+11 pts) but fully subsumed by
  the gap filter; never selected on top of it.
- Displacement >= 1.5 / 1.8 ATR: marginal IS (64-69% across buckets), never
  selected. Grade A already requires strong displacement.
- Midday block (12:00-13:59 ET): IS hours 12-13 ran 54-56%, but the filter
  was never selected once gap >= 1.0 was in — midday losers were mostly
  small-gap trades.
- Trending-day requirement (efficiency >= 0.5): no IS signal (68.0% trend vs
  65.1% chop). Chop was not the problem.
- Unmitigated-FVG-only: no IS signal (65.7% vs 66.7%).
- Session-hours filter, drop BTC / EUR/USD: BTC ran 72.7% IS and EUR/USD 9/9
  IS — dropping them (an old hypothesis) would have been wrong.

### Current best honest config (75.2% OOS wr, 125 trades, avg R 0.133)

grade A FVG only; entry at FVG CE limit (TTL 1h); stop beyond FVG far edge
+0.1 ATR; TP 0.5R all-out; skip if run-from-open >= 1.0 ATR; FVG gap >= 1.0
ATR; all hours; symbols Gold, EUR/USD, USD/JPY, BTC/USD, TSLA (GBP/USD, SPX,
SPY dropped); max 2 trades/symbol/day; 6-bar cooldown; conservative tie=loss.
Same config was rank-1 in-sample across three grid sizes (9216, 18432,
27648) — the selection is stable, not a knife-edge cell.

### Honest accounting

- 7 total OOS gate-checks were spent across the round's three sweeps (3+2+2);
  grid extensions between sweeps were driven only by in-sample tables.
- 80% OOS was NOT reached. Gap: 75.2% vs 80%. The Wilson 95% lower bound of
  the OOS result (94/125) is 66.8% — comfortably a real edge over coin-flip
  + costs, but the point estimate itself is an upper bound of true edge
  given grid-wide information sharing.

## Round 4 (backtest_chart_v4.py, reports/chart_backtest_round4.json, 2026-07-09)

Baseline entering the round: 75.2% OOS on 125 trades (round-3 winner, legacy
5m engine). Round result: that number is RETRACTED as an engine artifact; the
new honest best is 77.4% OOS on 62 trades, avg R 0.118, on a different entry
style that survives every honesty check. Data frozen 2026-07-08 (5m + real 2m
sub-bars), 77 days, split 2026-06-01, same anti-overfit gate.

### Confirmed

1. THE FILL-BAR WIN CREDIT WAS AN ANTI-CONSERVATIVE ENGINE ARTIFACT, AND IT
   WAS MOST OF THE MEASURED EDGE. The v2-v4 legacy engine credited a win when
   the CE limit fill and the target landed inside the SAME 5m bar, silently
   assuming the fill happened first. 47/59 of the incumbent's IS wins were
   such fill-bar wins. Scoring the identical trades with real 2m sub-bars
   ordering the intra-bar events (2m covers the whole OOS half): 17 of 94 OOS
   wins die, 0 of 31 OOS losses revive; OOS falls from 75.2% wr / +0.133 avg R
   to 61.6% / -0.069 — BELOW the 66.7% breakeven of TP 0.5R all-out. Strict
   5m scoring (deny all fill-bar wins) says 54.4% / -0.177. Independent 1m
   data confirmed 11/11 checkable disputed wins were fake (target touched
   BEFORE the fill). The incumbent CE-limit config has NO honest edge.
2. ROUND-3 LESSON 4 INVERTED. The "bar-granularity tie" losses were not
   heartbreaker near-wins, and finer-than-5m execution does not rescue them:
   with real 2m ordering not one incumbent OOS loss resolved favorably.
   Execution-side truth made the numbers WORSE, not better. The MFE>=90%
   pattern was largely the same artifact seen from the loss side (price
   touching the target region before/without a realistic fill).
3. WHY THE ARTIFACT STEERED EVERY EARLIER ROUND: it inflates precisely the
   configs whose target sits within one 5m bar of the entry - tight targets,
   tight stops, limit entries. That is exactly what rounds 2-3 "discovered"
   (CE limit over market entry, 0.5R over 0.7R). Those comparative rulings
   are retracted where they leaned on the artifact. An early round-4 sweep
   under the legacy engine "reached 80.0% OOS" (TP 0.4R, gap>=1.1, max
   1/day) - also retracted, same artifact.
4. THE HONEST SURVIVOR IS MARKET ENTRY AT THE NEXT 5M OPEN, which has no
   fill-order ambiguity by construction: legacy, strict and 2m-ordered
   engines agree on every one of its trades, and 44/44 1m-checkable OOS
   trades reproduced exactly (0 mismatches). Winning config: grade A FVG,
   gap >= 1.1 ATR, market entry next 5m open, stop beyond FVG far edge +0.1
   ATR, TP 0.4R all-out, all hours, no extension filter, drop BTC/USD,
   GBP/USD, Gold, SPX (honest IS win rates 50-59%), max 1 trade/symbol/day,
   6-bar cooldown. IS 80.0% on 60; OOS 77.4% on 62, avg R 0.118 (+7.3R).
   Rank-1 in-sample under the honest engine; first gate check held; the
   +0.2-ATR-buffer sibling also held (75.8% / 62 / 0.095) - stable family,
   only 2 OOS peeks spent on the walk.
5. HONEST SYMBOL MAP (strict scoring, IS half, no-drop replay): TSLA 100%
   (n=8), EUR/USD 80% (5), USD/JPY 67% (3), BTC/USD 59% (29), GBP/USD 57%
   (21), Gold 52% (21), SPX 50% (10), SPY 40% (5). The four dropped symbols
   were selected in-sample and the drop held OOS.

### Caveats on the new best (do not oversell it)

- 62 OOS trades is barely above the 60-trade floor; Wilson 95% lower bound
  of 48/62 is 65.6%, below the 71.4% breakeven of 0.4R all-out. The edge is
  positive point-estimate (+7.3R) but NOT yet statistically safe. It needs
  forward confirmation before sizing up.
- ~0.8 signals/day total across EUR/USD, USD/JPY, TSLA, SPY.
- Cumulative OOS-look ledger now 14 (7 round 3, 2 legacy-engine gate checks
  this round, 3 fixed-config engine measurements, 2 honest gate checks).

### Current best honest config (77.4% OOS wr, 62 trades, avg R 0.118)

grade A FVG only; market entry at next 5m open (no retrace wait); stop beyond
FVG far edge +0.1 ATR; TP 0.4R all-out; no extension filter; FVG gap >= 1.1
ATR; all hours; symbols EUR/USD, USD/JPY, TSLA, SPY (BTC/USD, GBP/USD, Gold,
SPX dropped); max 1 trade/symbol/day; 6-bar cooldown; conservative tie=loss
(and 1m/2m-verified scoring).

## Round 5 (backtest_chart_v5.py, reports/chart_backtest_round5.json, 2026-07-09)

Baseline entering the round: 77.4% OOS on 62 trades, avg R 0.118 (round-4
winner). Round result: 77.8% OOS on 63 trades, avg R 0.118 (+7.44R) — a
marginal but honest gain from a new filter set built on the incumbent's IS
loss anatomy. Same frozen data (2026-07-08), same split (2026-06-01), same
gate. The v5 replay of the incumbent reproduces the round-4 report trade for
trade before anything else runs.

### Confirmed

1. OVERNIGHT FX CHASES WERE THE LOSS FACTORY. IS hour band 00-06 ET ran
   46.7% (7/15) vs 91.3% (07-11), 91.7% (12-13), 90.0% (14-23); 8 of the
   incumbent's 12 IS losses lived there (EUR/USD and USD/JPY momentum chases
   in the dead pre-US session). "Entries only 07:00 ET or later" is part of
   the winning filter set that held OOS.
2. THE GAP FILTER NEEDS A CAP, NOT JUST A FLOOR. IS gap 1.1-2.2 ATR ran
   88.1% (37/42); gap >= 2.2 ran 61.1% (11/18). Monster gaps are blowoff
   chases with 4.5-9 ATR stops. The selected cap (gap < 3.0 ATR) held OOS.
3. DO NOT CHASE A FULLY-TRENDED DAY. IS day-efficiency >= 0.85 ran 57.1%
   (4/7) vs 93.5% below 0.6. The day_eff < 0.85 cap is in the winning set.
4. THE ROUND-4 SPX DROP WAS AN ENTRY-STYLE ARTIFACT, NOT A SYMBOL FACT.
   Under market entry SPX runs 81.2% IS (13/16) vs 50% under the retracted
   CE entry. SPX is back in and the add-back held OOS (SPX contributed 30 of
   the 131 trades).
5. STRUCTURAL SELECTION LESSON: WILSON-WIN-RATE RANKING MECHANICALLY CROWNS
   THE TIGHTEST TP A GRID OFFERS (0.5R in r3, 0.4R in r4, 0.3R here). The
   0.3R procedural winner "held the gate" at 77.8% OOS but with avg R 0.04 —
   within noise of the 76.9% breakeven a 0.3R target imposes. The identical
   filter set at TP 0.4R: same 77.8% OOS wr, avg R 0.118, +7.44R. The win
   rate bump from a tighter TP is breakeven-shifting, not edge. A fixed-TP
   ablation under a pre-committed decision rule (avg R >= 0.05 floor) is now
   a required final step of every sweep.
6. LOSS ANATOMY AT THE FRONTIER: of the new best's 14 OOS losses, 0 are
   tie-bars, none reached 90% of target (max MFE 65%), and 7 of 14 never got
   past 10% of the target. 2m-verified rescore changes 0 of 131 verdicts.
   These are wrong-direction entries that look identical to winners
   in-sample (mostly EUR/USD 07-11 ET, mid gap, mid efficiency). No
   execution-side rescue exists and no tested feature separates them — this
   dataset is near its honest ceiling; the path to 80% is forward data, not
   more filters.

### Rejected candidates (tested, did NOT earn a place)

- Chase cap (skip if price already >= 1.6 ATR beyond CE): real solo IS
  signal (71.9% vs ~89%) but never selected — subsumed by hours + eff + gap
  cap.
- Run-from-open cap 8 ATR (IS >= 8 ran 50%): present in several top-10 IS
  configs, not in rank-1; redundant once hours/eff/cap are in.
- pd_ok required (IS 88.9 vs 72.7) and sweep required (86.1 vs 70.8): real
  solo IS signals under market entry, never selected at the top; note both
  flipped sign vs the round-3 CE-entry findings — feature value is
  entry-style dependent.
- perday 2: dilutes IS to 74.5%.
- TP 0.3R / 0.35R: breakeven-shifting (lesson 5).
- Dropping EUR/USD: 7 of the best's 14 OOS losses are EUR/USD, but IS gives
  no honest justification (68-77% IS) and the trade count would fall below
  the 60-trade floor. Watch it forward.

### Current best honest config (77.8% OOS wr, 63 trades, avg R 0.118, +7.44R)

grade A FVG only; market entry at next 5m open (no retrace wait); stop beyond
FVG far edge +0.1 ATR; TP 0.4R all-out; FVG gap >= 1.1 ATR and < 3.0 ATR;
skip if day efficiency >= 0.85; entries only 07:00 ET or later; symbols
EUR/USD, USD/JPY, SPX, TSLA, SPY (BTC/USD, GBP/USD, Gold dropped); max 1
trade/symbol/day; 6-bar cooldown; conservative tie=loss (2m-verified, 0
verdict changes). IS 86.8% on 68 trades, avg R 0.239.

### Honest accounting

- 5 new OOS looks this round (2 gate checks, 1 pre-committed fixed-TP
  ablation, 2 fixed-config 2m measurements); cumulative ledger 19.
- 80% OOS NOT reached: 77.8% vs 80%. OOS Wilson 95% lower bound is 66.1%,
  still below the 71.4% breakeven of 0.4R all-out — the edge is
  point-estimate positive (+7.44R) but not statistically safe. The IS-OOS
  spread (86.8 -> 77.8) says most of the IS gain was selection optimism;
  the filters did not degrade OOS and improved it marginally on every axis
  (wr, trades, total R).

## Round 6 (backtest_chart_v6.py, reports/chart_backtest_round6.json, 2026-07-09)

Baseline entering the round: 77.8% OOS on 63 trades, avg R 0.118 (round-5
winner). Round result: 79.0% OOS on 62 trades, avg R 0.122 (+7.57R) — one
net loss fewer, accepted only because it passed the pre-committed
replacement rule (OOS wr > 77.8 AND OOS avg R >= 0.05). Same frozen data
(2026-07-08), same split (2026-06-01), same gate; the v6 replay reproduces
the round-5 best trade for trade before anything else runs; TP was FIXED at
0.4r a priori (round-5 lesson 5) so Wilson ranking could not crown a
tighter target.

### Confirmed

1. OVERSIZED STOPS AND OVEREXTENDED DAYS WERE THE LAST REMOVABLE LOSSES.
   IS tables on the round-5 winner: est. stop distance >= 3.6 ATR ran 71.4%
   (5/7, totR 0.00) and run-from-open >= 8 ATR ran 50.0% (2/4, totR -1.2).
   Both caps are in the new winning set (est-risk cap uses the decision-time
   estimate |close - stop|/ATR, so it is implementable live). Gap floor
   relaxed 1.1 -> 1.0 to restore trade count. Full set: grade A, gap in
   [1.0, 3.0) ATR, run-from-open < 8 ATR, day-eff < 0.85, est risk < 3.6
   ATR, entries >= 07:00 ET, drop BTC/GBP/Gold, max 1/symbol/day. IS 87.3%
   on 71; OOS 79.0% on 62, avg R 0.122; 2m rescore changes 0 of 133.
2. THE GAIN MECHANISM IS ROTATION, NOT SUBSETTING: vs the incumbent, 42
   trades removed and 44 added. OOS: the caps deleted 24 trades (19 wins, 5
   losses incl. the 4.2-ATR-risk TSLA eod bleed and the 17-ATR-run USD/JPY
   chase) and the 1.0 gap floor added 23 (19 wins, 4 losses). Net exactly
   one loss fewer — marginal, but every axis improved (wr, avg R, totR,
   Wilson LB 66.1 -> 67.4).
3. THE 60-TRADE OOS FLOOR IS NOW THE BINDING CONSTRAINT, NOT WIN RATE. 4 of
   6 gate peeks failed ONLY on count while posting 78.9-81.1% wr. The
   tightest sibling (gap >= 1.1, hours >= 08 ET, both caps) posted 81.1% on
   53 OOS trades — over the 80% target but under the pre-committed floor.
   It is a forward-watch candidate, NOT a claim; at 53-62 trades, one trade
   is ~1.7 wr points, so differences inside the family are single-trade
   noise.
4. FIXING TP A PRIORI KILLED THE TP-CROWNING BIAS CLEANLY: rank-1..15 IS
   are all 0.4r by construction, no ablation dance needed, and the
   procedural winner was directly comparable to the incumbent.

### Rejected candidates (tested in the IS tables, did NOT earn a place)

- Day-efficiency floor 0.15 (dead-chop skip): real IS solo signal (75.0%,
  6/8) but never selected at the top; subsumed by the risk/run caps.
- Hours >= 08 ET: in the grid (IS hr 8-9 ran 5/7) but every ge8 config that
  held wr failed the 60-trade floor.
- No IS signal at all this round (fixed off a priori): pd_ok, sweep
  required, unmitigated-only, FVG age, chase cap (chase >= 2.4 ran 91.7%
  IS), bars-left floor, entry-hour cap, dow filters.
- Dropping EUR/USD (weakest IS symbol at 75.0%, and 62% OOS in the new
  best): paired with the gap-1.0 relaxation it still cannot reach 60 IS/OOS
  trades. Watch forward; not actionable on this dataset.
- Weak-bias-only filter (IS 91.4% strong vs 81.8% weak): unusable — cuts IS
  to 35 trades.

### Current best honest config (79.0% OOS wr, 62 trades, avg R 0.122, +7.57R)

grade A FVG only; market entry at next 5m open (no retrace wait); stop
beyond FVG far edge +0.1 ATR; TP 0.4R all-out; FVG gap >= 1.0 ATR and < 3.0
ATR; skip if run-from-open >= 8 ATR; skip if day efficiency >= 0.85; skip if
est. stop distance >= 3.6 ATR; entries only 07:00 ET or later; symbols
EUR/USD, USD/JPY, SPX, TSLA, SPY (BTC/USD, GBP/USD, Gold dropped); max 1
trade/symbol/day; 6-bar cooldown; conservative tie=loss (2m-verified, 0
verdict changes). IS 87.3% on 71 trades, avg R 0.232.

### Honest accounting — and why this dataset is now exhausted

- 7 new OOS looks this round (6 gate checks, 1 fixed-config 2m
  measurement); cumulative ledger 26.
- 80% OOS NOT reached: 79.0% vs 80%. OOS Wilson 95% lower bound 67.4%,
  still below the 71.4% breakeven of 0.4R all-out.
- Exhaustion evidence, stated with numbers: (a) every tighter sibling that
  clears 80% point-estimate wr dies on the 60-trade OOS floor, and the OOS
  half is fixed at 39 days (~0.8 signals/day) — no rule can add trades to a
  frozen dataset; (b) the surviving family's wr spread (77.6-81.1) equals
  1-2 trades, inside single-trade noise at n=53-62; (c) 0 of 133 verdicts
  change under real 2m sub-bars — no execution-granularity headroom
  remains; (d) the new best's 9 IS losses show no unused separator in any
  printed table (strongest leftover, weak-bias, would cut IS to 35 trades).
  Further "gains" on this data would be fitting the one-trade margin. The
  path to an honest 80% is forward data: run the round-6 config live or on
  new frozen weeks, and track the 81.1%/53-trade ge8 sibling as the
  promotion candidate once it can clear 60 trades.
