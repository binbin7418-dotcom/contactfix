# ContactFix — vCard Duplicate Merge & Repair (Original Never Overwritten)

ContactFix takes the `.vcf` contacts file you exported from iPhone,
Android, Google or Outlook and does the three jobs vendors usually sell
as separate tools: it **finds duplicate contacts**, **merges them
field-by-field so nothing is lost**, and **repairs damaged entries**
(garbled vCard 2.1 encodings, missing names, mixed 2.1/3.0/4.0 versions).
It writes a brand-new cleaned file — **your original is never
overwritten** — plus a report whose re-check section proves the result:
duplicate groups remaining, contact-count conservation, and a
field-level merge log. 100% local — your address book never leaves
your computer.

## The safety promise
- The original file is never modified or overwritten. Output is always
  a new `<name>.cleaned.vcf` file next to a `<name>.contactfix-report.md`.
- Merging is a union, not a pick-one-and-pray: every distinct phone,
  email, address and note survives. When single-value fields conflict
  (two different job titles), the kept value goes in the card and the
  other value is written verbatim into the merge log.
- **Same email does not mean same person.** Two contacts that share an
  email address but carry different names are NOT merged — they are
  listed in the report's "Needs your review" section with the reason.
  The same applies to same-name contacts with no shared phone or email.

## How matching works (deterministic, no AI)
- **Phone**: punctuation stripped, country code unified — `+86 138-…`,
  `tel:+86138…` and a bare `138…` are the same number. The assumed
  country code for national-format numbers is configurable (default 86).
- **Email + name**: a shared email merges only when a name also matches.
- **Name**: display names are compared normalised (case/space/punctuation
  insensitive, including reversed family/given order from the N field).
- Repairs before matching: quoted-printable and base64 values are
  decoded (UTF-8, GBK/GB18030 and other declared charsets), folded lines
  are rejoined, `tel:` URIs are normalised, missing FN is rebuilt from
  N (or email/phone), and the whole output is normalised to vCard 3.0,
  the version every contacts app still imports.

## Verified behaviour
Seeded acceptance test with known ground truth — a 9-card file mixing
vCard 2.1/3.0/4.0 (one person in 3 cards with fields spread across them,
two different people sharing one email, two quoted-printable Chinese
names, a card with no FN) and a 500-card file with 40 planted duplicate
pairs: **36/36 checks passed**. Small file: 9 in → 7 out, 8 repairs,
exactly 1 group merged, the shared-email pair held back for review,
re-check 0 groups remaining, field-union check passed, original file
byte-identical. Big file: 40/40 groups found, 500 → 460, re-check
clean, 0.28 s on the test machine. Full log: TEST-RESULTS.txt.

## Run from source
```
python contact_fix.py scan  contacts.vcf
python contact_fix.py fix   contacts.vcf --out cleaned_folder
python contact_fix.py fix   contacts.vcf --cc 1
```
No dependencies — Python standard library only. The Windows app
(`gui.py`, packaged with PyInstaller) wraps the same engine.

## Scope & honest limits
- Input: `.vcf` text files with vCard 2.1 / 3.0 / 4.0 cards (iCloud,
  Google Contacts, Outlook and Android exports covered by the seed
  dialects). A file that is not vCard text at all is reported, not
  force-converted.
- Output is vCard 3.0 (UTF-8). Properties ContactFix does not know are
  carried through as generic union fields, so vendor extensions are not
  silently dropped, but exotic parameter encodings (e.g. RFC 6868
  caret-encoding inside parameter values) are passed through verbatim
  rather than interpreted.
- Photos and other binary fields are carried as opaque bytes; if two
  cards in a group carry *different* photos, the first is kept and the
  merge log says so — review it there.
- Matching is deterministic rules, not fuzzy AI. Two entries for the
  same person that share no phone, no email and
  no identical display name will not be merged (they may appear in the
  review list); two different people with the same display name and a
  shared phone line (e.g. a family landline on both cards) WILL be
  grouped by the shared-phone rule — the merge log shows the exact
  reason for every group, so check it before importing.
