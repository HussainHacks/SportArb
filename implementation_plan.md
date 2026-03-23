# Profitability Improvements — Revised Plan

Budget: **5,000 req/hr**. Target: **~2,640 req/hr** (leaves headroom for retries, diagnostics, future features).

## Phase 1: Structural Detection Fixes (Must-Fix Before Anything Else)

These bugs cause the arb detector to **miss valid arbs** or **produce false results**. No amount of coverage expansion helps if the detector itself is broken.

---

### 1.1 Spread Grouping Across Lines

**Bug:** [_build_market_key](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/engine/market_index.py#287-292) uses the exact point value, so Book A's `+6.5` and Book B's `+7.0` land in **separate groups** and never get compared.

```python
# Current: "event:spreads:full_game:6.5" ≠ "event:spreads:full_game:7.0"
return f"{odds.event_id}:{odds.market_key.value}:{odds.period}:{line_part}"
```

**Fix:** For spreads, group by event+market+period only (no line). The arb detector's [_are_complementary_selections](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/engine/arb_detector.py#74-108) already validates that `point_A == -point_B`, so mismatched lines will be rejected correctly at detection time. For totals, keep the line in the key since Over 220.5 vs Under 221.5 is never valid.

#### [MODIFY] [market_index.py](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/engine/market_index.py#L287-L292)

```python
@staticmethod
def _build_market_key(odds: NormalizedOdds) -> str:
    if odds.market_key.value == "spreads":
        # Spreads: group all lines together so cross-line arbs are detected
        return f"{odds.event_id}:{odds.market_key.value}:{odds.period}:all"
    line_part = f"{odds.point}" if odds.point is not None else "none"
    return f"{odds.event_id}:{odds.market_key.value}:{odds.period}:{line_part}"
```

---

### 1.2 Top-N Fallback Combinations Not Used

**Bug:** [_get_top_n_odds_by_selection](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/engine/arb_detector.py#33-60) collects top-3 per selection, but [find_2way_arbs](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/engine/arb_detector.py#127-222) only ever checks index `[0]` vs `[0]`. If the best pair is same-operator, it gives up instead of trying `[0]` vs `[1]`.

#### [MODIFY] [arb_detector.py](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/engine/arb_detector.py#L158-L168)

After the same-operator check at index [0], try fallback combinations from the top-N list:

```python
# Try pairs from top-N, not just [0] vs [0]
for a_candidate in top_n_by_selection[selections[i]]:
    for b_candidate in top_n_by_selection[selections[j]]:
        if a_candidate.operator == b_candidate.operator:
            continue  # skip same-operator
        # found a cross-operator pair → proceed to complementary check
        best_a, best_b = a_candidate, b_candidate
        break
    else:
        continue
    break
else:
    same_operator += 1
    continue
```

---

### 1.3 Tracker Called Per-Arb Instead of Per-Group

**Bug:** In [main.py](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/main.py), `arb_tracker.process_arbs(key, [arb])` is called once per individual arb. The tracker should receive the **full filtered batch** for a group so it can correctly detect when an arb disappears (empty list).

#### [MODIFY] [main.py](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/main.py#L369-L384)

```python
if arbs:
    t_alert_start = time.monotonic()
    filtered = apply_all_filters(arbs)
    diag.filtered_arbs += len(filtered)
    # Pass the FULL filtered list for this group to the tracker
    alertable = arb_tracker.process_arbs(key, filtered)
    for a in alertable:
        paper_recorder.record_arb(a)
        try:
            await telegram_service.send_arb_alert(a)
        except Exception:
            pass
    diag.alert_ms += (time.monotonic() - t_alert_start) * 1000
else:
    arb_tracker.process_arbs(key, [])
```

---

### 1.4 Snapshot Reconciliation

**Bug:** When MarketIndex holds stale odds from a previous cycle and new odds arrive only for some operators, the arb detector compares fresh prices against stale ones. The freshness gap filter catches some of these, but not all.

**Fix:** On each upsert batch, tag every updated group with the current cycle number. In [recompute_group](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/engine/arb_detector.py#359-396), skip legs whose timestamp is older than the current cycle's start time — don't mix stale and fresh data.

#### [MODIFY] [market_index.py](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/engine/market_index.py)

Add a `cycle_timestamp` parameter to [upsert_odds](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/engine/market_index.py#168-203) and store it on each group as `last_cycle_ts`. Then in `MarketGroup.get_all_odds()`, optionally filter legs older than a threshold.

---

## Phase 2: Config Cleanup + Normalizer Fixes

---

### 2.1 Move Bookmaker List to Config

#### [MODIFY] [main.py](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/main.py#L283)

```diff
-        bookmakers=["Circa", "Bally Bet", "DraftKings", "FanDuel", "Bet365"],
+        bookmakers=settings.odds_api_io_bookmakers,
```

#### [MODIFY] [config.py](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/config.py#L42-L45)

```diff
-        default=["DraftKings", "FanDuel", "BetMGM", "Caesars", "ESPN BET", "BetRivers"],
+        default=["Circa", "Bally Bet", "DraftKings", "FanDuel", "Bet365"],
```

---

### 2.2 Add Circa + Bally Bet Normalizer Mappings

#### [MODIFY] [odds_api_io_normalizer.py](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/collectors/odds_api_io_normalizer.py#L36-L63)

Add to `BOOKMAKER_CANONICAL`:
```diff
+    "circa": "circasports",
+    "bally bet": "ballybet",
```

Add to `DISPLAY_NAMES`:
```diff
+    "ballybet": "Bally Bet",
```

#### [MODIFY] [config.py](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/config.py#L54-L70)

```diff
+            "ballybet",           # Bally Bet
```

---

### 2.3 Lower Min Arb Threshold

#### [MODIFY] [.env](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/.env)

```diff
-MIN_ARB_PERCENT=0.5
+MIN_ARB_PERCENT=0.25
```

---

## Phase 3: Smart Event Selection (API Budget Optimization)

Instead of blindly polling the first N events, **rank events by arb potential** and spend requests on the most promising ones.

---

### 3.1 Hot-Event Ranking

#### [NEW] [event_ranker.py](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/engine/event_ranker.py)

Score each event from the `/events` response based on:
- **Bookmaker count**: events listing 3+ bookmakers score highest
- **Commence proximity**: events starting in 1–60 minutes score higher (odds move most)
- **Previous arb history**: events that produced arbs before get priority

Sort by score descending, take the top 10.

---

### 3.2 Conservative Multi-Sport with Budget Guard

3 sports, 10 events/sport, 45s interval = **2,640 req/hr**.

#### [MODIFY] [.env](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/.env)

```diff
+ODDS_API_IO_SPORTS=["basketball","ice-hockey","american-football"]
```

#### [MODIFY] [main.py](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/main.py#L292)

```diff
-    poll_interval = 35
+    poll_interval = 45
```

Add a request counter that enforces a hard ceiling:

```python
# Hard ceiling: refuse to fetch if approaching 4,500 req/hr
if io_collector.total_requests > 4500:
    logger.warning("Approaching API limit, skipping cycle")
    await asyncio.sleep(poll_interval)
    continue
```

---

### 3.3 Post-Fetch Noise Reduction

> [!NOTE]
> This does **not** save API calls (cost already paid). It reduces noise in the MarketIndex so the arb detector doesn't waste time on single-bookmaker events.

After normalizing, skip events with fewer than 2 operators from entering `MarketIndex.upsert_odds()`.

---

## Phase 4: WebSocket Activation

The single biggest profitability change. REST latency ~30–60s → WS latency ~1–2s.

#### [MODIFY] [main.py](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/main.py)

Add `run_odds_api_io_ws_loop()`:
1. Creates [OddsApiIoWsCollector](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/collectors/odds_api_io_ws.py#30-273) with `on_odds_update` → `MarketIndex.upsert_odds` → [recompute_group](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/engine/arb_detector.py#359-396) → filter → alert
2. `on_odds_delete` → `MarketIndex.remove_operator_from_event()`
3. REST backfill every 60s for cache maintenance
4. Toggle via `ODDS_API_IO_MODE=websocket` in [.env](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/.env)

---

## Verification Plan

### Automated Tests
```
python -m pytest backend/tests/ -v
```

Key test files:
- [test_market_index.py](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/tests/test_market_index.py) — verify spread grouping fix
- [test_arb_detector.py](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/tests/test_arb_detector.py) — verify top-N fallback combos
- [test_odds_api_io_normalizer.py](file:///c:/Users/ahmed/OneDrive/Desktop/sportsarb/backend/tests/test_odds_api_io_normalizer.py) — verify Circa/Bally Bet mapping

### Manual Verification
1. Start bot, observe 3 sports scanning
2. Check `books=[...]` includes `circasports` and/or `ballybet`
3. Verify `same-operator` rejections decrease (fallback combos working)
4. Check request count stays under 3,000/hr
5. Watch for increased `candidate_arbs` count
