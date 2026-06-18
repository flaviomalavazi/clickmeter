# queries.csv format

Two files live here:

- **`queries.csv.example`** — a starter set of 15 query shapes (point lookups, range scans, aggregations, quantiles, `LIKE`, conditional counts, etc.). Copy it to `queries.csv` and edit:
  ```bash
  cp queries/queries.csv.example queries/queries.csv
  ```
- **`queries.csv`** — the file actually consumed by the JMX. Edit this one.

One row per query *instance*. Columns:

| column         | purpose                                                                 |
| -------------- | ----------------------------------------------------------------------- |
| query_template | SQL string. May reference `${p1}`..`${p5}` placeholders.                |
| p1..p5         | Values bound at request time. Leave blank if unused.                    |

The JMX uses `${__eval(${query_template})}` so the placeholders inside the SQL
are resolved against the values JMeter pulled from the row.

Quoting rules:
- Wrap the whole `query_template` cell in double quotes (the CSV reader has `quotedData=true`).
- Inside the SQL, quote string literals as usual: `'${p1}'`.
- Numeric params don't need quotes: `user_id = ${p2}`.

`shareMode=all` + `recycle=true` means rows are consumed across all VUs and the
file loops forever — so you can drive 40k concurrent threads off a relatively
small CSV. If you want each thread to see a unique row, change `shareMode` to
`shareMode.thread` in the JMX.
