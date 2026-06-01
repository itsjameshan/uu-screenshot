import keyring, imaplib, email

pw = keyring.get_password("uu_screenshot_smtp", "22440318@qq.com")
imap = imaplib.IMAP4_SSL("imap.qq.com", 993)
imap.login("22440318@qq.com", pw)
imap.select("INBOX")

status, uid_data = imap.uid("SEARCH", None, "ALL")
all_uids = [int(uid) for uid in uid_data[0].split()] if uid_data[0] else []
print(f"Total emails: {len(all_uids)}")
print(f"UIDs: {all_uids}")

if all_uids:
    last_few = all_uids[-5:]
    print(f"\nLast 5 emails (by UID):")
    for uid in last_few:
        status, data = imap.uid("FETCH", str(uid), "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
        if status == "OK":
            for part in data:
                if isinstance(part, tuple):
                    msg = email.message_from_bytes(part[1])
                    subject = msg.get("Subject", "")
                    print(f"  UID {uid}: {subject}")

                    if "screenshot" in subject.lower():
                        print(f"    >>> TRIGGER KEYWORD MATCH! <<<")

imap.logout()