# Hand-check instructions (`handcheck.csv`)

20 funds picked with seed 2026: 10 that have a Form D in our data, 10 from the rest, each from a different adviser with at most 25 in-scope funds (so the fund is on the first IAPD page). About 2 minutes each.

For each row:

1. **GAV and file number**: open `iapd_url` → *Part 1* of the latest Form ADV → Schedule D, Section 7.B.(1). Find the fund by its PFID (`805-…`).
   - `harry_gav_ok`: does question 11 (gross asset value) equal `our_gav_usd`?
   - `harry_file_no_ok`: is `our_form_d_file_no` one of the numbers in question 22? Blank `our_form_d_file_no` is correct if question 22 is empty or a placeholder like `021-999999`.
2. **Amount sold**: open `edgar_url`.
   - If it ends in `primary_doc.xml`, it opens the Form D itself as a page. Scroll to **Item 13, Offering and Sales Amounts**: `harry_amount_ok` = does *Total Amount Sold* equal `our_latest_amount_sold_usd`?
   - If it is a list of filings (a `browse-edgar` link), we found no Form D for that number: write `y` if EDGAR shows none, else note what it shows.
   - If `edgar_url` is blank (fund has no file number), write `y`.
3. Use `y` / `n`, and put anything odd in `notes`. Our data is the latest ADV filed by 2026-10-31; if IAPD shows a newer filing, note it rather than marking `n`.
