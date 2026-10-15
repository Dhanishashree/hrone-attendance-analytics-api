# Decisions

1. **Indexes.** The unique employee-code index enforces identity, and the unique employee/date attendance index makes duplicate punch-ins race-safe. The department/join-date index supports trend headcounts; the joined-date index supports unfiltered monthly headcount. Attendance indexes support date-and-code sorting, employee/month lookups, status filters, and the late leaderboard's date/late filter. I considered a separate email index but rejected it because no endpoint searches by email.
2. **Punch-in race.** Both requests can read the employee, but neither checks for an existing log before insertion. MongoDB's unique (`emp_code`, `date`) index serializes the conflict: one insert succeeds with 201; the duplicate-key loser returns 409.
3. **Ties.** The leaderboard uses standard `$rank` on total late minutes, then filters ranks at or below `limit`. Every tied employee at the cutoff is returned; employee code orders ties.
4. **Headcount.** The department aggregation begins with employees who joined by month end, then looks up and unwinds logs while preserving employees with no matching log.
5. **100x data.** I would benchmark the actual query plans and workload, then add pagination or precomputed daily summaries where they reduce repeated aggregation without weakening the source-of-truth rules.
