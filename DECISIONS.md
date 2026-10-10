# DECISIONS.md

1. **Indexes.** I created a unique index on `emp_code` to prevent duplicate employees and a compound unique index on `(emp_code, date)` to prevent multiple attendance records for the same employee on one date. I added indexes for attendance date and employee, date and status, and date and late minutes to support listing and analytics queries. I also indexed employee joining dates and department plus joining date to support headcount queries. I considered indexing every field but rejected that approach because extra indexes increase storage and write costs.

2. **Punch-in race.** Two identical requests can both check for an existing record before either inserts. The unique `(emp_code, date)` index ensures that only one record can be stored. The other request must be handled as a duplicate-key conflict rather than creating a second record.

3. **Ties.** The leaderboard uses ranking by late minutes and then sorts by employee code for stable ordering. Employees tied at the cutoff may be affected by the limit, so the returned list contains at most the requested number of employees.

4. **Headcount.** The department summary starts from employees who joined by the end of the month and looks up their attendance logs. Employees with no matching logs remain in the results and are counted.

5. **One change at 100x data.** I would benchmark realistic workloads and review aggregation execution plans, then consider pre-aggregating frequently requested analytics to reduce repeated computation.
