# Admin rights for Walter

Walter runs as the `walter` user. Out of the box he can't use `sudo` for
anything except setting the time zone and starting an update. There are
three ways to give him more, and each one needs the owner, not Walter.

## 1. At first-time setup (phone page)

The setup page you open by scanning the QR code has two optional boxes:

- **Admin password for the laptop**: lets *you* log in as `walter` (e.g. SSH
  to `walter.local`) and use `sudo` with that password.
- **Give Walter full admin rights on this computer (passwordless sudo)**:
  off unless you tick it. Walter can then install software and change system
  settings without asking anyone for a password.

  The trade-off, honestly: Walter reads web pages and email, so a malicious
  page or message could try to trick him into doing something harmful, and
  with admin rights the damage isn't limited to his own files. Only tick it
  on a laptop that's used just for Walter, keep backups of anything that
  matters, and remember you can take it back by deleting
  `/etc/sudoers.d/90-walter`.

Both are applied by a root helper (`deploy/admin-password.sh`, installed as
`/usr/local/sbin/walter-admin-password` and `walter-admin`) that Walter can't
edit. It runs **once**: after the first request, even an empty one, the door
closes for good (`/var/lib/walter/admin-password-handled`), so Walter can't
use it to give himself rights later.

## 2. Later, with a USB stick (physical presence)

If setup is done and you skipped it, use any FAT32 or exFAT USB stick:

1. Put a plain text file called `walter-admin.txt` on it containing:
   ```
   full admin: yes
   ```
   Other lines you can use: `full admin: no` (take the rights away) and
   `password: your-new-password` (8+ characters; sets the `walter` login
   password).
2. With Walter **running**, plug the stick in. Within a few seconds it's
   done, and the file is deleted from the stick.
3. Check: Walter can run `sudo -n true && echo OK`. The result (never the
   password) is logged in `/var/lib/walter/admin.log`.

Safety rules in the helper: sticks already plugged in at boot are ignored
(Walter could have written to one before restarting), only real USB
hardware counts (not loop devices or other virtual disks), only FAT/exFAT
is read, and it's mounted `nosuid,nodev,noexec`.

## 3. Later, as root

From any root shell (including the recovery shell in the next section):

```
walter-admin status
walter-admin grant      # writes /etc/sudoers.d/90-walter (checked with visudo)
walter-admin revoke     # deletes it
```

## Locked out completely?

If the laptop was installed before these helpers existed, or you have no
root shell at all, you can still do it by hand at the keyboard by booting
with `init=/bin/bash`: see `docs/admin-recovery.md`.

## Taking it back

`sudo rm /etc/sudoers.d/90-walter` (or `walter-admin revoke`, or a stick with
`full admin: no`). Nothing else changes.
