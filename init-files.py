gitignore = """# ── Virtual environment ──────────────────────────────────────────────────────
anybox-obsidian-env/

# ── Input data (AnyBox export) ───────────────────────────────────────────────
data/*
!data/.gitkeep

# ── Output / staging area ────────────────────────────────────────────────────
staging/*
!staging/.gitkeep

# ── Log files ────────────────────────────────────────────────────────────────
logs/*
!logs/.gitkeep

# ── Cookies (sensitive — never commit) ───────────────────────────────────────
*.cookies
medium.com_cookies.txt

# ── macOS ────────────────────────────────────────────────────────────────────
.DS_Store
__MACOSX/

# ── Python ───────────────────────────────────────────────────────────────────
__pycache__/
*.py[cod]
*.pyo
"""

import os

# Write .gitignore
with open('.gitignore', 'w', encoding='utf-8') as f:
    f.write(gitignore)

# Create the .gitkeep placeholder files so folders are tracked by git
for folder in ['data', 'staging', 'logs']:
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, '.gitkeep'), 'w') as f:
        pass

print('.gitignore created')
print('.gitkeep files created in data/, staging/, logs/')