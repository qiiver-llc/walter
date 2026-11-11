# Give Walter admin rights (one time, at the keyboard)

For a Walter laptop whose admin password step was skipped: no password,
no root login, so nobody can use `sudo`. About 5 minutes. You need a USB
keyboard plugged into the laptop.

## Step 1: Stop at the boot menu

1. Restart the laptop (or power it on).
2. As soon as the logo goes away, **tap the Down arrow** over and over.
   The boot menu only shows for 1 second. Missed it? Restart and try again.
   (If no menu appears at all, tap **Esc** instead.)
3. With **Debian GNU/Linux** highlighted, press **e**.

## Step 2: Boot straight to a root shell

1. Move down to the line that starts with **linux**.
2. Press **End** to jump to the end of that line.
3. Type a space, then:  **init=/bin/bash**
4. Press **Ctrl+X** (or **F10**) to boot.
5. You get a prompt like  **root@(none):/#**

## Step 3: Type these, one line at a time

```
mount -o remount,rw /
echo 'walter ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/90-walter
chmod 0440 /etc/sudoers.d/90-walter
/usr/sbin/visudo -c -f /etc/sudoers.d/90-walter
```

The last line must say **parsed OK**.
If it doesn't: `rm /etc/sudoers.d/90-walter` and type the echo line again.

## Step 4: Restart

```
sync
mount -o remount,ro /
reboot -f
```

If `reboot -f` complains, hold the power button for 5 seconds. That's safe
now: everything is saved.

## Step 5: Check

Walter boots as normal. Ask him to run:

```
sudo -n true && echo OK
```

**OK** means it worked.

## If you know a root password

(Only if one was set when Debian was installed; normally there isn't.)
Press **Ctrl+Alt+F2**, log in as **root**, and type the Step 3 lines
(skip the mount line), then **Ctrl+Alt+F1** to go back. Or from any
login: `su -`, then the same lines. No restart needed.

## Prefer a password instead of no-password sudo?

In Step 3, instead of the echo/chmod/visudo lines, type `passwd walter`
and pick a password. Walter is already in the sudo group, so `sudo` then
works with that password (Walter would have to be told it).

## Good to know

- **GRUB password:** if pressing **e** asks for a user name and password,
  GRUB was locked. Debian doesn't do that by default.
- **BIOS password:** not needed; we don't change any BIOS settings.
- **No menu at all?** Boot any Debian live or installer USB, choose
  rescue mode, and do Step 3 on the laptop's disk.
- **Encrypted disk?** You'd type the disk passphrase first, as usual.
- **Trade-off:** Walter reads web pages and email; a malicious page could
  try to trick him. With admin rights the damage isn't limited to his
  own files. Keep backups.

## Undo

```
sudo rm /etc/sudoers.d/90-walter
```

That's all: Walter goes back to no admin rights.

Newer Walter installs can do this without a keyboard: an opt-in box on the
setup page, a USB stick with `walter-admin.txt`, or `walter-admin grant`
as root (see `docs/admin-access.md`).
