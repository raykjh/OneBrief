# Operations workbook rules

The CSV is the authoritative row set. Keep every `request_id` exactly once.

- A request is overdue when `due_date` is before the workbook's documented as-of date
  and status is neither `done` nor `cancelled`.
- Risk is **Critical** for an overdue high-impact request, **High** for any other
  overdue request or a blocked high-impact request, **Medium** for a high-impact open
  request or any request due within three calendar days, and **Normal** otherwise.
- Never invent a numerical score. Sort risk using the explicit order Critical, High,
  Medium, Normal and then by due date and request ID.
- Workload is the sum of `effort_hours` for requests that are not done or cancelled.
- Invalid dates, duplicate IDs, missing owners, negative effort, and unknown status or
  impact values belong in an error sheet and must not be silently repaired.
- The workbook must document its as-of date and formulas and must not contain macros or
  external workbook links.
