"""Validated, atomic import plans for custom trigger information."""

import hashlib
import json
import re

from .triggered_memory import MAX_ENTRIES, make_entry, normalize

MAX_IMPORT_BYTES = 256 * 1024


def revision(entries):
    return hashlib.sha256(json.dumps(entries, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def content_digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def parse_import(text):
    if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_IMPORT_BYTES:
        raise ValueError('Choose a UTF-8 Markdown or JSON file no larger than 256 KB.')
    text = text.lstrip('\ufeff').replace('\r\n', '\n').strip()
    if not text:
        raise ValueError('Choose the trigger list file first.')
    if text.startswith(('[', '{')):
        try:
            rows = json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise ValueError('The JSON file is invalid.') from exc
        if isinstance(rows, dict):
            rows = rows.get('entries')
        if not isinstance(rows, list):
            raise ValueError('JSON must contain a list of entries, or an object with an entries list.')
    else:
        # The generated list has numbered sections, each containing three
        # fenced text fields. Ignore its introduction and index, not entries.
        sections = re.split(r'^##\s+\d+\.\s+[^\n]+\n', text, flags=re.MULTILINE)[1:]
        if not sections:
            raise ValueError('No trigger entries found. Use the supplied Name / Trigger words or phrases / Information Markdown format.')
        rows = []
        for number, section in enumerate(sections, 1):
            fields = {}
            for label in ('Name', 'Trigger words or phrases', 'Information'):
                pattern = r'^\*\*' + re.escape(label) + r'\*\*\s*\n+```(?:text)?[ \t]*\n(.*?)\n```[ \t]*(?:\n|$)'
                found = re.findall(pattern, section, flags=re.MULTILINE | re.DOTALL)
                if len(found) != 1:
                    raise ValueError(f'Entry {number}: missing or repeated {label} block.')
                fields[label] = found[0]
            rows.append({'name': fields['Name'], 'phrases': fields['Trigger words or phrases'].splitlines(),
                         'text': fields['Information']})
    if not 1 <= len(rows) <= MAX_ENTRIES:
        raise ValueError(f'Import between 1 and {MAX_ENTRIES} entries at a time.')
    result, seen = [], set()
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict) or not isinstance(row.get('name'), str) or not isinstance(row.get('text'), str):
            raise ValueError(f'Entry {number}: name and text must be strings.')
        phrases = row.get('phrases')
        if not isinstance(phrases, list) or any(not isinstance(p, str) for p in phrases):
            raise ValueError(f'Entry {number}: phrases must be a list of strings.')
        if row.get('memory_id') is not None:
            raise ValueError(f'Entry {number}: importing linked memories is not supported; use custom information.')
        if not isinstance(row.get('enabled', True), bool):
            raise ValueError(f'Entry {number}: enabled must be true or false.')
        try:
            entry = make_entry(row['name'], '\n'.join(phrases), row['text'], enabled=row.get('enabled', True))
        except ValueError as exc:
            raise ValueError(f'Entry {number}: {exc}') from exc
        key = normalize(entry['name'])
        if key in seen:
            raise ValueError(f'Entry {number}: duplicate imported name {entry["name"]!r}.')
        seen.add(key)
        result.append(entry)
    return result


def suggested_target(imported, existing):
    """Match the generated bilingual names to the existing English names."""
    name = normalize(imported['name'])
    exact = [e for e in existing if normalize(e['name']) == name]
    candidates = exact or [e for e in existing
                           if normalize(e['name'].split(' / ', 1)[0]) == name.split(' / ', 1)[0]]
    if len(candidates) == 1 and not candidates[0].get('memory_id'):
        return candidates[0]['id']
    # Ambiguous matches and scoped linked memories need an explicit choice.
    return 'skip' if candidates else 'new'


def build_import_plan(existing, imported, targets):
    if len(targets) != len(imported):
        raise ValueError('Preview the import and choose an action for every entry.')
    current = [dict(e) for e in existing]
    indexes = {e['id']: i for i, e in enumerate(current)}
    used = set()
    counts = {'added': 0, 'updated': 0, 'skipped': 0}
    for incoming, target in zip(imported, targets):
        if target == 'skip':
            counts['skipped'] += 1
            continue
        entry = dict(incoming)
        if target == 'new':
            if any(normalize(e['name']) == normalize(entry['name']) for e in current):
                raise ValueError(f'{entry["name"]}: an entry with that name exists. Choose Replace or Skip.')
            current.append(entry)
            counts['added'] += 1
        elif target in indexes:
            if target in used:
                raise ValueError('Two imported entries cannot replace the same existing entry.')
            used.add(target)
            old = current[indexes[target]]
            # Updating facts must not silently re-enable an existing trigger.
            entry.update(id=old['id'], enabled=old.get('enabled', True))
            current[indexes[target]] = entry
            counts['updated'] += 1
        else:
            raise ValueError('A selected replacement no longer exists. Preview the import again.')
    if len(current) > MAX_ENTRIES:
        raise ValueError(f'This would create {len(current)} entries; the limit is {MAX_ENTRIES}. Replace or skip some entries.')
    names = [normalize(e['name']) for e in current]
    changed_names = {normalize(e['name']) for e, t in zip(imported, targets) if t != 'skip'}
    if any(names.count(name) > 1 for name in changed_names):
        raise ValueError('The import would create duplicate names. Adjust the replacement choices.')
    return current, counts
