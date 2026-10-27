# ContactFix Report — seed_small.vcf

- Input: 9 contacts (2.1×2, 3.0×5, 4.0×2)
- Mode: fix
- Repairs applied: 8
  - card #2: TEL tel:-URI normalised to a plain number
  - card #6: decoded N from QUOTED-PRINTABLE
  - card #6: decoded FN from QUOTED-PRINTABLE
  - card #6: decoded NOTE from QUOTED-PRINTABLE
  - card #7: decoded N from QUOTED-PRINTABLE
  - card #7: decoded FN from QUOTED-PRINTABLE
  - card #8: missing FN generated from N: 'Mei Chen'
  - 1 output card(s): N synthesised from FN for vCard 3.0 consumers
- Duplicate groups merged: 1
  - Group: 3 cards merged (Zhang Wei, 张伟, Wei Zhang); matched on: same email zhangwei@example.com + same name; same phone +8613800112233
    FN kept: 'Zhang Wei'; other forms in log: 'Wei Zhang', '张伟'
    N kept: 'Wei;Zhang;;;'; other forms in log: 'Zhang;Wei;;;', '张;伟;;;'
    TEL: 1 unique value(s) kept, 2 format variant(s) unified
    EMAIL: 1 unique value(s) kept
    ORG: kept 'Example Corp' (from card #2)
    TITLE: kept 'Engineer' (from card #2)
    NOTE: 1 note(s) combined
- Needs your review (NOT merged): 1 pair(s)
  - Li Na <-> Li Qiang: same email shared-family@example.com, different names — not merged, your call

## Re-check (output re-parsed and re-scanned)
- Output contacts: 7 (expected 7) — conservation: OK
- Duplicate groups remaining: 0
- Output cards missing FN: 0
- Field-union check: OK — nothing lost
- New errors introduced: 0
- Cleaned file written (new file, original untouched): seed_small.cleaned.vcf
- Elapsed: 0.00s — 100% local, nothing uploaded.
