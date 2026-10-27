#!/usr/bin/env python3
"""ContactFix desktop shell (tkinter). Pick a .vcf file -> scan for
duplicates and damage, or fix: merge duplicates losslessly, repair damaged
entries, write a NEW cleaned file (the original is never overwritten) and
a re-check report."""
import os
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from contact_fix import run_fix, run_scan, DEFAULT_CC


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ContactFix — vCard Duplicate Merge & Repair "
                   "(Original Never Overwritten)")
        self.geometry("760x580")
        self.file = tk.StringVar()
        self.cc = tk.StringVar(value=DEFAULT_CC)
        tk.Label(self, text="Contacts file (.vcf) exported from iPhone, "
                            "Android, Google or Outlook:").pack(
            anchor="w", padx=12, pady=(12, 2))
        row = tk.Frame(self)
        row.pack(fill="x", padx=12)
        tk.Entry(row, textvariable=self.file).pack(side="left", fill="x",
                                                   expand=True)
        tk.Button(row, text="Browse…", command=self.pick).pack(side="left",
                                                               padx=6)
        row2 = tk.Frame(self)
        row2.pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(row2, text="Country code assumed for numbers without one "
                            "(86 = China, 1 = US/Canada, 44 = UK):").pack(
            side="left")
        tk.Entry(row2, textvariable=self.cc, width=5).pack(side="left",
                                                           padx=6)
        btns = tk.Frame(self)
        btns.pack(pady=12)
        self.btn_scan = tk.Button(btns, text="Scan Only — Report, "
                                             "Write Nothing",
                                  command=lambda: self.run(False), height=2)
        self.btn_scan.pack(side="left", padx=6)
        self.btn_fix = tk.Button(btns, text="Fix — Merge, Repair & "
                                            "Re-Check",
                                 command=lambda: self.run(True), height=2)
        self.btn_fix.pack(side="left", padx=6)
        self.log = scrolledtext.ScrolledText(self, wrap="word")
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.write("ContactFix finds duplicate contacts (same phone, or "
                   "same email + same name), merges them field by field so "
                   "nothing is lost, repairs damaged entries (garbled "
                   "vCard 2.1 encodings, missing names, mixed versions), "
                   "and re-checks the result: duplicate groups remaining, "
                   "contact-count conservation, field-by-field merge log.\n"
                   "SAFETY PROMISE: your original file is never "
                   "overwritten — ContactFix always writes a new "
                   "'.cleaned.vcf' file plus the report. Pairs that look "
                   "like two different people sharing one email are NOT "
                   "merged; they are listed for your review.\n"
                   "Everything runs locally — your contacts never leave "
                   "this computer.\n")

    def write(self, s):
        self.log.insert("end", s)
        self.log.see("end")

    def pick(self):
        p = filedialog.askopenfilename(
            filetypes=[("vCard files", "*.vcf"), ("All files", "*.*")])
        if p:
            self.file.set(p)

    def run(self, fix):
        path = self.file.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showerror("ContactFix", "Choose a .vcf file first.")
            return
        cc = self.cc.get().strip() or DEFAULT_CC
        if not cc.isdigit():
            messagebox.showerror("ContactFix",
                                 "Country code must be digits, e.g. 86.")
            return
        self.btn_scan.config(state="disabled")
        self.btn_fix.config(state="disabled")
        threading.Thread(target=self._work, args=(path, cc, fix),
                         daemon=True).start()

    def _work(self, path, cc, fix):
        try:
            if fix:
                outdir = os.path.join(
                    os.path.dirname(os.path.abspath(path)),
                    "contactfix_output")
                s = run_fix(path, outdir, cc)
                v = s["verify"]
                self.write(
                    f"\nDone. Contacts in: {len(s['cards'])} · groups "
                    f"merged: {len(s['groups'])} · repairs: "
                    f"{len(s['repairs'])} · pairs left for your review: "
                    f"{len(s['review'])}\n"
                    f"Re-check: contacts out: {v['actual_output']} "
                    f"(expected {v['expected_output']}) · duplicate groups "
                    f"remaining: {v['remaining_groups']} · fields lost: "
                    f"{len(v['union_failures'])} · new errors: "
                    f"{v['new_errors']}\n"
                    f"Cleaned file: {s['output_path']}\n"
                    f"Report: {s['report_path']}\n"
                    "Your original file was not touched.\n")
            else:
                s = run_scan(path, cc)
                self.write(
                    f"\nScan done. Contacts: {len(s['cards'])} · duplicate "
                    f"groups: {len(s['groups'])} · redundant copies: "
                    f"{s['redundant_copies']} · repairs that would be "
                    f"applied: {len(s['repairs'])} · pairs needing your "
                    f"review: {len(s['review'])}\n"
                    "Scan only — nothing was written.\n")
        except Exception as e:  # surface, never crash silently
            self.write(f"\nError: {e}\n")
        finally:
            self.btn_scan.config(state="normal")
            self.btn_fix.config(state="normal")


if __name__ == "__main__":
    App().mainloop()
