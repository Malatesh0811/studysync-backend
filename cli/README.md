# StudySync

**Offline-first, distributed workspace synchronisation for developers.**

Share and sync files across laptops over a local network or the internet — with cryptographic integrity checking and conflict protection built in.

---

## Install

```bash
pip install studysync
```

That's it. No environment setup, no config files, no server URL required.

---

## Quick Start (2 steps)

**Step 1 — Install**
```bash
pip install studysync
```

**Step 2 — Join a workspace with your token**
```bash
study join <TOKEN>
```

You'll receive a `<TOKEN>` from whoever created the workspace. After joining, pull all shared files straight to your current directory:

```bash
study pull
```

---

## Full Workflow

### Create a workspace (team lead / project owner)
```bash
study workspace create my-project
# Output includes a TOKEN — share it with your team
```

### Join an existing workspace (everyone else)
```bash
study join <TOKEN>
study pull          # downloads all files into your current directory
```

### Push a file
```bash
study push path/to/file.py
```

If someone else pushed a newer version since your last pull, you'll see:

```
⚠  CONFLICT — Remote has changes. Pull first.
```

Pull, resolve, then push again.

### Check sync status
```bash
study status
```

Shows `CLEAN`, `MODIFIED`, `DELETED`, or `UNTRACKED` for every file in your local workspace.

---

## Self-Hosting

Advanced users running their own StudySync backend can override the default server:

```bash
study join <TOKEN> --server https://my-backend.example.com
```

The URL is saved locally after the first use — subsequent commands pick it up automatically.

---

## How It Works

| Feature | Detail |
|---|---|
| **Integrity** | Every file is SHA-256 hashed before push and verified after pull |
| **Conflict protection** | Optimistic Concurrency Control (OCC) — the server rejects a push if remote has a newer version |
| **Offline-first** | Edits happen locally; the network is only touched on explicit `push` / `pull` |
| **Zero-payload server** | File bytes stream directly between client and storage; the server only manages metadata |
| **Silent history** | Every push is versioned server-side — no data is ever permanently overwritten |

---

## License

MIT © Adinath
