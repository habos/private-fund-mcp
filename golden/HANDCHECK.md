# Hand-check instructions (`handcheck.csv`)

20 funds picked with seed 2026: 10 that have a Form D in our window, 10 from the rest. About 2 minutes each.

For each row:

1. **GAV and file number**: open `iapd_url` → *Part 1* of the latest Form ADV → Schedule D, Section 7.B.(1). Find the fund by its PFID (`805-…`).
   - `harry_gav_ok`: does question 11 (gross asset value) equal `our_gav_usd`?
   - `harry_file_no_ok`: is `our_form_d_file_no` one of the numbers in question 22? Blank `our_form_d_file_no` is correct if question 22 is empty or a placeholder like `021-999999`.
2. **Amount sold**: open `edgar_url`.
   - If it is a filing folder, open the primary doc: `harry_amount_ok` = does *Total Amount Sold* (Item 13) equal `our_latest_amount_sold_usd`? Also check it is the newest Form D for that file number.
   - If it is a file-number list (no Form D in our data), check whether EDGAR shows any Form D for that number, and write `y` if it shows none or note what it shows.
3. Use `y` / `n`, and put anything odd in `notes`. Our data is the latest ADV filed by 2026-10-31; if IAPD shows a newer filing, note it rather than marking `n`.
