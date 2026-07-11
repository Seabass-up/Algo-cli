use std::collections::HashSet;
use std::env;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

const MAX_INDEX_TEXT: usize = 4_000;

struct SourceRoot {
    harness: &'static str,
    kind: &'static str,
    root: PathBuf,
    patterns: Vec<&'static str>,
    max_files: usize,
}

struct Record {
    id: String,
    harness: String,
    kind: String,
    title: String,
    path: String,
    relative_path: String,
    description: String,
    tags: Vec<String>,
    updated: String,
    file_size: u64,
    file_mtime_ns: u128,
    links: Vec<String>,
    summary: String,
    search_text: String,
}

fn main() {
    if let Err(err) = run() {
        eprintln!("harness-indexer error: {err}");
        std::process::exit(1);
    }
}

fn run() -> io::Result<()> {
    let output = parse_output_arg()?;
    let home = home_dir()?;
    let config_dir = resolve_config_dir(&home);
    let repo_dir = resolve_repo_dir(&home);
    let roots = source_roots(&home, &config_dir, &repo_dir);
    let mut records = Vec::new();

    for root in &roots {
        for path in iter_files(root)? {
            if let Ok(record) = make_record(root, &path) {
                records.push(record);
            }
        }
    }

    if let Some(parent) = output.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(output, render_index(&roots, &records))?;
    Ok(())
}

fn parse_output_arg() -> io::Result<PathBuf> {
    let mut args = env::args().skip(1);
    while let Some(arg) = args.next() {
        if arg == "--output" {
            if let Some(value) = args.next() {
                return Ok(PathBuf::from(value));
            }
        }
    }
    Err(io::Error::new(
        io::ErrorKind::InvalidInput,
        "usage: harness-indexer --output PATH",
    ))
}

fn home_dir() -> io::Result<PathBuf> {
    env::var_os("USERPROFILE")
        .or_else(|| env::var_os("HOME"))
        .map(PathBuf::from)
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "home directory not found"))
}

/// Resolve config directory with rebrand dual-support (mirrors Python config.py).
/// Precedence: ALGO_CLI_CONFIG_DIR > OLLAMA_CLI_CONFIG_DIR > ~/.algo_cli (preferred) > ~/.ollama_cli
fn resolve_config_dir(home: &Path) -> PathBuf {
    if let Some(explicit) = env::var_os("ALGO_CLI_CONFIG_DIR") {
        return PathBuf::from(explicit);
    }
    if let Some(explicit) = env::var_os("OLLAMA_CLI_CONFIG_DIR") {
        return PathBuf::from(explicit);
    }
    let new_dir = home.join(".algo_cli");
    if new_dir.exists() {
        return new_dir;
    }
    new_dir
}

/// Resolve algo-cli repo directory (mirrors Python _algo_cli_repo_dir).
fn resolve_repo_dir(home: &Path) -> PathBuf {
    let candidates = ["algo-cli", "ollama-cli"];
    for name in &candidates {
        let candidate = home.join(name);
        if candidate.exists() {
            return candidate;
        }
    }
    home.join("algo-cli")
}

fn source_roots(home: &Path, config_dir: &Path, repo_dir: &Path) -> Vec<SourceRoot> {
    vec![
        // algo-cli config roots (match Python harness.py SOURCE_ROOTS exactly)
        root(
            "algo-cli",
            "skill",
            config_dir.join("skills"),
            vec!["*.md"],
            200,
        ),
        root(
            "algo-cli",
            "model",
            config_dir.join("models"),
            vec!["*.md"],
            200,
        ),
        root(
            "algo-cli",
            "x_search",
            config_dir.join("x_search_cache"),
            vec!["*.md"],
            150,
        ),
        root(
            "algo-cli",
            "tool",
            repo_dir.join("algo_cli"),
            vec![
                "xai_*.py",
                "x_account.py",
                "model_info.py",
                "main.py",
                "tools.py",
                "harness.py",
            ],
            80,
        ),
        root(
            "algo-cli",
            "tool",
            repo_dir.join("tests"),
            vec!["test_xai*.py", "test_x_account.py"],
            40,
        ),
        root(
            "algo-cli",
            "memory",
            repo_dir.join("personal"),
            vec!["*.md"],
            40,
        ),
        // Third-party agent roots (unchanged)
        root(
            "codex",
            "skill",
            home.join(".codex/skills"),
            vec!["SKILL.md"],
            300,
        ),
        root(
            "codex",
            "tool",
            home.join(".codex/scripts"),
            vec!["*.py", "*.ps1", "*.cmd", "*.bat"],
            100,
        ),
        root(
            "codex",
            "memory",
            home.join(".codex/memories"),
            vec!["*.md"],
            120,
        ),
        root(
            "codex",
            "extension",
            home.join(".codex/plugins/cache"),
            vec!["SKILL.md"],
            250,
        ),
        root(
            "claude",
            "skill",
            home.join(".claude/skills"),
            vec!["SKILL.md"],
            80,
        ),
        root(
            "claude",
            "extension",
            home.join(".claude/plugins"),
            vec!["SKILL.md"],
            500,
        ),
        root(
            "openclaw",
            "skill",
            home.join(".openclaw/skills"),
            vec!["SKILL.md"],
            120,
        ),
        root(
            "openclaw",
            "skill",
            home.join(".openclaw/plugin-skills"),
            vec!["SKILL.md"],
            120,
        ),
        root(
            "openclaw",
            "prompt",
            home.join(".openclaw/workspace"),
            vec![
                "AGENTS.md",
                "SOUL.md",
                "TOOLS.md",
                "USER.md",
                "HEARTBEAT.md",
                "IDENTITY.md",
                "lessons-learned.md",
                "LESSONS-LEARNED.md",
            ],
            40,
        ),
        root(
            "openclaw",
            "prompt",
            home.join(".openclaw/sandboxes"),
            vec![
                "AGENTS.md",
                "SOUL.md",
                "TOOLS.md",
                "USER.md",
                "HEARTBEAT.md",
                "IDENTITY.md",
                "lessons-learned.md",
                "LESSONS-LEARNED.md",
            ],
            200,
        ),
        root(
            "openclaw",
            "prompt",
            home.join(".openclaw/agents"),
            vec![
                "AGENTS.md",
                "SOUL.md",
                "TOOLS.md",
                "USER.md",
                "HEARTBEAT.md",
                "IDENTITY.md",
                "lessons-learned.md",
                "LESSONS-LEARNED.md",
            ],
            120,
        ),
        root(
            "openclaw",
            "wiki",
            home.join(".openclaw/workspace/wiki"),
            vec!["*.md"],
            700,
        ),
        root(
            "openclaw",
            "memory",
            home.join(".openclaw/memory"),
            vec!["*.md", "*.json"],
            80,
        ),
        root(
            "openclaw",
            "extension",
            home.join(".openclaw"),
            vec!["openclaw.json", "plugins/installs.json"],
            20,
        ),
        root(
            "agents",
            "skill",
            home.join(".agents/skills"),
            vec!["SKILL.md"],
            120,
        ),
        root(
            "mercury",
            "skill",
            home.join(".mercury/skills"),
            vec!["SKILL.md"],
            80,
        ),
        root(
            "mercury",
            "prompt",
            home.join(".mercury/soul"),
            vec!["*.md"],
            40,
        ),
        root(
            "mercury",
            "workflow",
            home.join(".mercury/harness"),
            vec!["*.md"],
            80,
        ),
        root(
            "cli-agent",
            "skill",
            home.join(".cli-agent/skills"),
            vec!["SKILL.md"],
            80,
        ),
        root(
            "pi",
            "prompt",
            home.join("pi-mono"),
            vec!["AGENTS.md", "README.md", "CONTRIBUTING.md", "package.json"],
            20,
        ),
        root(
            "pi",
            "tool",
            home.join("pi-mono/packages"),
            vec!["package.json", "*.md"],
            160,
        ),
    ]
}

fn root(
    harness: &'static str,
    kind: &'static str,
    root: PathBuf,
    patterns: Vec<&'static str>,
    max_files: usize,
) -> SourceRoot {
    SourceRoot {
        harness,
        kind,
        root,
        patterns,
        max_files,
    }
}

fn iter_files(root: &SourceRoot) -> io::Result<Vec<PathBuf>> {
    let mut seen = Vec::new();
    if !root.root.exists() {
        return Ok(seen);
    }
    walk(root, &root.root, &mut seen)?;
    seen.sort_by_key(|path| path.to_string_lossy().to_lowercase());
    Ok(seen)
}

fn walk(root: &SourceRoot, current: &Path, seen: &mut Vec<PathBuf>) -> io::Result<()> {
    if seen.len() >= root.max_files {
        return Ok(());
    }
    let entries = match fs::read_dir(current) {
        Ok(entries) => entries,
        Err(_) => return Ok(()),
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().to_string();
        if path.is_dir() {
            if !skip_part(&name) {
                walk(root, &path, seen)?;
            }
        } else if matches_patterns(root, &path, &name) && !should_skip(&path) {
            seen.push(path);
            if seen.len() >= root.max_files {
                break;
            }
        }
    }
    Ok(())
}

fn matches_patterns(root: &SourceRoot, path: &Path, filename: &str) -> bool {
    let rel = path
        .strip_prefix(&root.root)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/");
    root.patterns.iter().any(|pattern| {
        if pattern.contains('/') {
            rel == *pattern
        } else {
            glob_name(filename, pattern)
        }
    })
}

fn glob_name(name: &str, pattern: &str) -> bool {
    if pattern == name {
        return true;
    }
    if let Some(suffix) = pattern.strip_prefix("*.") {
        return name.ends_with(&format!(".{suffix}"));
    }
    false
}

fn skip_part(name: &str) -> bool {
    matches!(
        name,
        ".git"
            | "node_modules"
            | ".venv"
            | "venv"
            | "__pycache__"
            | ".tmp"
            | "tmp"
            | "logs"
            | "sessions"
            | "archive"
            | "fixtures"
            | "test"
            | "tests"
            | "Email"
    )
}

fn should_skip(path: &Path) -> bool {
    let name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("")
        .to_lowercase();
    // Match Python SECRET_RE exactly: whole-word boundaries around secret-like names
    if [
        "secret",
        "token",
        "credential",
        "password",
        "passwd",
        ".env",
    ]
    .iter()
    .any(|needle| name.contains(needle))
    {
        return true;
    }
    // Match Python SECRET_RE: api_key, api-key, access_token, access-token, private_key, private-key, authorization
    if name.contains("api_key")
        || name.contains("api-key")
        || name.contains("access_token")
        || name.contains("access-token")
        || name.contains("private_key")
        || name.contains("private-key")
        || name.contains("authorization")
    {
        return true;
    }
    // Also skip if any path component is a skip directory
    path.components()
        .filter_map(|component| component.as_os_str().to_str())
        .any(skip_part)
}

fn make_record(root: &SourceRoot, path: &Path) -> io::Result<Record> {
    let metadata = fs::metadata(path)?;
    let text = read_text(path);
    let title = frontmatter_value(&text, "title")
        .or_else(|| frontmatter_value(&text, "name"))
        .or_else(|| first_heading(&text))
        .unwrap_or_else(|| {
            path.file_stem()
                .and_then(|value| value.to_str())
                .unwrap_or("record")
                .to_string()
        });
    let description = frontmatter_value(&text, "description").unwrap_or_default();
    let tags = frontmatter_tags(&text);
    let relative_path = path
        .strip_prefix(&root.root)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/");
    let id = format!("{}:{}:{}", root.harness, root.kind, relative_path);
    let updated = system_time_string(metadata.modified().unwrap_or(SystemTime::UNIX_EPOCH));
    let links = wiki_links(&text);
    let summary = summary_text(&text);
    let search_text = format!(
        "{} {} {} {} {} {} {} {}",
        id,
        root.harness,
        root.kind,
        title,
        description,
        tags.join(" "),
        relative_path,
        summary
    )
    .to_lowercase();

    Ok(Record {
        id,
        harness: root.harness.to_string(),
        kind: root.kind.to_string(),
        title,
        path: path.to_string_lossy().to_string(),
        relative_path,
        description,
        tags,
        updated,
        file_size: metadata.len(),
        file_mtime_ns: mtime_ns(&metadata),
        links,
        summary,
        search_text,
    })
}

fn read_text(path: &Path) -> String {
    let bytes = match fs::read(path) {
        Ok(bytes) => bytes,
        Err(_) => return String::new(),
    };
    let text = String::from_utf8_lossy(&bytes);
    text.chars().take(MAX_INDEX_TEXT).collect()
}

fn frontmatter_value(text: &str, key: &str) -> Option<String> {
    if !text.starts_with("---") {
        return None;
    }
    let end = text[3..].find("\n---")? + 3;
    for line in text[3..end].lines() {
        let Some((candidate, value)) = line.split_once(':') else {
            continue;
        };
        if candidate.trim() == key {
            return Some(
                value
                    .trim()
                    .trim_matches('"')
                    .trim_matches('\'')
                    .to_string(),
            );
        }
    }
    None
}

fn frontmatter_tags(text: &str) -> Vec<String> {
    let value = match frontmatter_value(text, "tags") {
        Some(value) => value,
        None => return Vec::new(),
    };
    value
        .trim_matches('[')
        .trim_matches(']')
        .split(',')
        .map(|item| item.trim().trim_matches('"').trim_matches('\'').to_string())
        .filter(|item| !item.is_empty())
        .collect()
}

fn first_heading(text: &str) -> Option<String> {
    text.lines().find_map(|line| {
        line.strip_prefix("# ")
            .map(|value| value.trim().to_string())
    })
}

fn wiki_links(text: &str) -> Vec<String> {
    let mut links = Vec::new();
    let mut seen = HashSet::new();
    let mut rest = text;
    while let Some(start) = rest.find("[[") {
        rest = &rest[start + 2..];
        if let Some(end) = rest.find("]]") {
            let raw = &rest[..end];
            let link = raw
                .split(['|', '#'])
                .next()
                .unwrap_or("")
                .trim()
                .to_string();
            if !link.is_empty() && seen.insert(link.clone()) {
                links.push(link);
            }
            rest = &rest[end + 2..];
        } else {
            break;
        }
        if links.len() >= 40 {
            break;
        }
    }
    links
}

fn summary_text(text: &str) -> String {
    let summary = text
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty() && !line.starts_with("---"))
        .collect::<Vec<_>>()
        .join(" ");
    summary.chars().take(500).collect()
}

fn system_time_string(time: SystemTime) -> String {
    let secs = time
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    secs.to_string()
}

#[cfg(unix)]
fn mtime_ns(metadata: &fs::Metadata) -> u128 {
    use std::os::unix::fs::MetadataExt;
    let secs = metadata.mtime();
    if secs < 0 {
        0
    } else {
        secs as u128 * 1_000_000_000 + metadata.mtime_nsec() as u128
    }
}

#[cfg(windows)]
fn mtime_ns(metadata: &fs::Metadata) -> u128 {
    use std::os::windows::fs::MetadataExt;
    const WINDOWS_TO_UNIX_EPOCH_100NS: u64 = 116_444_736_000_000_000;
    u128::from(
        metadata
            .last_write_time()
            .saturating_sub(WINDOWS_TO_UNIX_EPOCH_100NS),
    ) * 100
}

fn render_index(roots: &[SourceRoot], records: &[Record]) -> String {
    let roots_json = roots
        .iter()
        .map(render_root)
        .collect::<Vec<_>>()
        .join(",\n    ");
    let records_json = records
        .iter()
        .map(render_record)
        .collect::<Vec<_>>()
        .join(",\n    ");
    format!(
        "{{\n  \"generated\": \"{}\",\n  \"record_count\": {},\n  \"roots\": [\n    {}\n  ],\n  \"records\": [\n    {}\n  ],\n  \"refresh_stats\": {{\"reused_records\": 0, \"rebuilt_records\": {}, \"removed_records\": 0}},\n  \"indexer\": \"rust\"\n}}\n",
        system_time_string(SystemTime::now()),
        records.len(),
        roots_json,
        records_json,
        records.len()
    )
}

fn render_root(root: &SourceRoot) -> String {
    format!(
        "{{\"harness\":\"{}\",\"kind\":\"{}\",\"root\":\"{}\",\"patterns\":[{}]}}",
        json_escape(root.harness),
        json_escape(root.kind),
        json_escape(&root.root.to_string_lossy()),
        root.patterns
            .iter()
            .map(|pattern| format!("\"{}\"", json_escape(pattern)))
            .collect::<Vec<_>>()
            .join(",")
    )
}

fn render_record(record: &Record) -> String {
    format!(
        "{{\"id\":\"{}\",\"harness\":\"{}\",\"kind\":\"{}\",\"title\":\"{}\",\"path\":\"{}\",\"relative_path\":\"{}\",\"description\":\"{}\",\"tags\":[{}],\"updated\":\"{}\",\"file_size\":{},\"file_mtime_ns\":{},\"links\":[{}],\"summary\":\"{}\",\"search_text\":\"{}\"}}",
        json_escape(&record.id),
        json_escape(&record.harness),
        json_escape(&record.kind),
        json_escape(&record.title),
        json_escape(&record.path),
        json_escape(&record.relative_path),
        json_escape(&record.description),
        render_string_array(&record.tags),
        json_escape(&record.updated),
        record.file_size,
        record.file_mtime_ns,
        render_string_array(&record.links),
        json_escape(&record.summary),
        json_escape(&record.search_text)
    )
}

fn render_string_array(items: &[String]) -> String {
    items
        .iter()
        .map(|item| format!("\"{}\"", json_escape(item)))
        .collect::<Vec<_>>()
        .join(",")
}

fn json_escape(value: &str) -> String {
    let mut escaped = String::new();
    for ch in value.chars() {
        match ch {
            '"' => escaped.push_str("\\\""),
            '\\' => escaped.push_str("\\\\"),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            ch if ch.is_control() => escaped.push_str(&format!("\\u{:04x}", ch as u32)),
            ch => escaped.push(ch),
        }
    }
    escaped
}
