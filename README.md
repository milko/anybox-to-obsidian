# AnyBox → Obsidian Importer

This project converts an **AnyBox JSON export** into **Obsidian-ready Markdown notes**.

It is designed for large libraries of saved links and focuses on turning each saved URL into a Markdown note with:

- frontmatter suitable for Obsidian
- extracted article content when possible
- preserved AnyBox folder structure
- cleaned tags for Obsidian
- Medium cookie support for paywalled/member-only articles
- chunked processing for large imports
- parallel fetching for better performance

## Project files

### Main script
- `anybox_to_obsidian_cookies.py`

This is the main importer. It:
- reads the AnyBox JSON export
- fetches each article URL
- tries to extract the article body
- writes one Markdown file per saved link
- uses cookies when available for Medium content

### Shell script
- `run-turbo.sh`

This is the convenience wrapper script for running the importer in chunks.

Based on your current setup, it is intended to accept these arguments:
1. `START`
2. `LIMIT`
3. `WORKERS`

So you can run imports in batches without editing the Python file each time.

### Cookie file
- `medium.com_cookies.txt`

This contains your exported browser cookies for Medium and allows the importer to access articles that would otherwise only return a preview for anonymous requests.

### Input data
- `AnyBoxExport.json` or whatever JSON export path you configured inside the script

This is the JSON export produced by AnyBox.

### Output folder
- `staging/`

This is where the generated Markdown notes are written.

## What the importer does

For each item in the AnyBox export, the script tries to:

1. read the link metadata from the JSON
2. fetch the article page
3. extract article content using one or more extraction methods
4. build Obsidian frontmatter
5. save the result as a Markdown file in the appropriate folder

The script currently includes support for:

- parallel requests using multiple workers
- retries for transient HTTP issues
- cookie-based authenticated fetching
- lead image extraction
- author extraction when available
- published date extraction when available
- Medium preview detection
- Obsidian-safe filenames
- folder normalization
- tag cleanup

## Frontmatter fields

Each generated note is written with YAML frontmatter similar to this:

```yaml
---
title: "Example title"
source: "https://example.com/article"
author: "Author Name"
published: "2024-01-15"
created: 2024-06-01
description: "Saved description from AnyBox"
lead_image: "https://example.com/image.jpg"
tags:
  - AI
  - longreads
---
```

### Field mapping
- `title` ← from AnyBox title
- `source` ← from the URL
- `created` ← from AnyBox `dateAdded`, formatted as `YYYY-MM-DD`
- `description` ← from AnyBox description
- `author` ← extracted from the webpage when possible
- `published` ← extracted from the webpage when possible
- `lead_image` ← extracted from page metadata or article images when available

## Tag behavior

The script converts AnyBox tags into Obsidian tags using the following rules:

- it reads the `tags` field from the AnyBox JSON item
- if a tag is stored as a nested path, it uses **only the last element**
- spaces are converted to hyphens
- duplicates are removed case-insensitively
- it does **not** automatically add a `clippings` tag

Example:

```text
["Reading", "Machine Learning"] -> Machine-Learning
["Work", "AI"] -> AI
```

## Folder behavior

The importer preserves the AnyBox folder structure as much as possible.

- if `folder` is a string, it uses that folder name
- if `folder` is a list/path, it joins the parts into nested folders
- unsafe path characters are removed
- if no folder exists, it falls back to `Imported`

## Filename behavior

Markdown filenames are based on the saved article title.

- original case is preserved
- invalid filesystem characters are removed
- filenames are truncated if needed
- each note is saved as `.md`

## Requirements

You need Python 3 installed.

### 1. Create the virtual environment

From the project root, run:

```bash
python3 -m venv anybox-obsidian-env
```

### 2. Activate the virtual environment

```bash
source anybox-obsidian-env/bin/activate
```

You should see `(anybox-obsidian-env)` in your terminal prompt.

### 3. Install the required libraries

```bash
pip install requests beautifulsoup4 trafilatura readability-lxml lxml
```

### 4. Deactivating when done

```bash
deactivate
```

> **Note:** Always activate the virtual environment before running the scripts. The `anybox-obsidian-env/` folder is excluded from Git via `.gitignore`.

### Required / optional notes
- `requests` and `beautifulsoup4` are required
- `trafilatura` is strongly recommended
- `readability-lxml` is a useful fallback
- if some optional packages are missing, the script will still run but with fewer extraction options

## Medium cookies

If a large portion of your saved articles are on Medium and behind a paywall, cookies are essential.

### Cookie file name
Your current setup uses:

```text
medium.com_cookies.txt
```

### Cookie file format
The script supports:
- Netscape cookie export format (`.txt` / `.cookies`)
- JSON cookie exports (`.json`)

### Where to put it
Place the cookie file in the location expected by the Python script, currently:

```text
./medium.com_cookies.txt
```

If the file exists and loads correctly, the script will use it automatically.

## Running the importer

### Option 1: run the Python script directly

Example:

```bash
python3 anybox_to_obsidian_cookies.py --start 0 --limit 100 --workers 4
```

### Arguments
- `--start` = zero-based starting index in the AnyBox export
- `--limit` = how many items to process in this batch
- `--workers` = number of parallel worker threads
- `--skip-existing` = skip notes that already exist

Example with skipping existing files:

```bash
python3 anybox_to_obsidian_cookies.py --start 100 --limit 100 --workers 4 --skip-existing
```

### Option 2: run the shell wrapper

If your `run-turbo.sh` wrapper is configured the way we discussed, it should take:

```bash
./run-turbo.sh START LIMIT WORKERS
```

Example:

```bash
./run-turbo.sh 0 100 4
```

This would process:
- starting at item `0`
- importing `100` items
- using `4` workers

Another example:

```bash
./run-turbo.sh 100 100 4
```

This would process the next batch of 100 items.

## Recommended workflow for a large library

For a large archive such as 1800+ links, process in chunks.

A practical workflow is:

1. run 100 items at a time
2. inspect results in `staging/`
3. check a few Medium articles specifically
4. continue with the next batch

Example sequence:

```bash
./run-turbo.sh 0 100 4
./run-turbo.sh 100 100 4
./run-turbo.sh 200 100 4
```

### Worker recommendations
- `4` workers = safe starting point
- `6–8` workers = faster, but more likely to trigger issues on some sites
- if a batch includes many Medium links, staying closer to `4` is usually safer

## Understanding results

The importer may classify items in a few different ways internally:

- **Success**: content was extracted and written to Markdown
- **Fallback**: the page was fetched but good article content could not be extracted
- **Skipped**: item had no URL or the output file already existed when skipping was enabled

### Important note about Medium
Without cookies, Medium may return only a preview. That preview can look like a successful extraction even though it is not the full article.

With cookies enabled, the importer has a much better chance of retrieving the full content for member-only articles.

## Suggested verification after each batch

After each run, check:

1. a few generated Markdown files
2. at least a couple of Medium articles
3. whether frontmatter fields look correct
4. whether tags look clean in Obsidian
5. whether the folder structure matches your expectations

## Typical directory layout

A typical working directory might look like this:

```text
.
├── anybox_to_obsidian_cookies.py
├── run-turbo.sh
├── medium.com_cookies.txt
├── AnyBoxExport.json
└── staging/
```

## Troubleshooting

### 1. Medium article only contains a preview
Possible causes:
- cookie file missing
- cookie file expired
- cookie file path in the script is wrong
- Medium login session is no longer valid

What to do:
- re-export cookies
- confirm the file is named `medium.com_cookies.txt`
- confirm the script path matches the actual filename

### 2. `FileNotFoundError` for cookies
The Python script is looking in the wrong path or the cookie filename does not match.

Check the `COOKIE_FILE` constant in `anybox_to_obsidian_cookies.py`.

### 3. Wrong folder structure
Some AnyBox items may store folder values differently.

The current script normalizes:
- strings
- lists
- empty values

If something still looks odd, inspect the original JSON for that item.

### 4. Bad filenames
The script removes characters that are unsafe for filesystems. If a title looks different from the original article title, this is usually why.

### 5. Tabs vs spaces issues
Your current Python script version is intentionally tab-indented for compatibility with your editor preferences.

If you edit the file manually, keep indentation consistent throughout.

### 6. Extraction is incomplete on some sites
This is normal. Different sites use different page structures, scripts, paywalls, and anti-bot measures.

Even with multiple extraction strategies, some articles will still fall back or import imperfectly.

## Resetting and re-running

If you want to start over:

1. delete the generated Markdown files inside `staging/`
2. keep your Python script
3. keep your shell script
4. keep your AnyBox JSON export
5. keep your cookie file
6. rerun from the first batch

Example:

```bash
rm -rf staging
mkdir staging
./run-turbo.sh 0 100 4
```

## Obsidian usage notes

Once imported, the generated Markdown files can be placed directly inside your Obsidian vault or copied from `staging/` into the vault.

Obsidian can then organize them via:
- folders
- tags
- links
- Bases / saved views

## Summary

This project is intended to make a large-scale migration from AnyBox to Obsidian practical by combining:

- batch processing
- resilient article extraction
- Medium cookie support
- clean Markdown output
- Obsidian-friendly metadata

If you continue evolving the importer, the most likely future improvements would be:
- richer logging
- better duplicate handling
- improved site-specific extraction rules
- optional failed-item export for retry runs
