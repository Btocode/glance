//! Harper, configured the way Glance ran `harper-ls --stdio` on a `plaintext` document.
//!
//! What harper-ls 3.3.0 did for Glance, and what this reproduces:
//! - `LintGroup::new_curated(dict, dialect)` with the curated rule set; Glance only ever
//!   switches rules off (`linters: {Rule: false}`), never on.
//! - The dictionary is Harper's curated FST dictionary merged with the personal dictionary
//!   (one word per line, every word tagged with the configured dialect, as
//!   `harper-dictionary-wordlist::load_dict` does).
//! - `Document::new(text, &PlainEnglish, &dict)`; texts over 120 000 bytes are not linted.
//! - Diagnostics: `organized_lints` (rules in key order), `remove_overlaps_map`, one result
//!   per remaining lint whose `rule` is the rule key.
//! - Replacements: harper-ls answers a code-action request for a diagnostic's range with the
//!   suggestions of *every* lint (before overlap removal, sorted by priority) overlapping the
//!   diagnostic's first character, and Glance's client took them all (deduplicated, in that
//!   order). So a word that is both a SpellCheck and a SplitWords lint offers both sets. That
//!   union is `replacements`; the lint's own suggestions are `suggestions`.
//!
//! Not reproduced (host state Glance never wanted): harper-ls's own `~/.config/harper-ls`
//! user dictionary, the workspace `.harper-dictionary.txt` of glanced's working directory,
//! per-file dictionaries and ignored-lint stores.
//!
//! All offsets are Unicode scalar values (`char`s), which is what a Python `str` indexes.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
use std::sync::Arc;

use harper_core::linting::{FlatConfig, Lint, LintGroup, Suggestion};
use harper_core::parsers::PlainEnglish;
use harper_core::spell::{FstDictionary, MergedDictionary, MutableDictionary};
use harper_core::{Dialect, DialectFlags, DictWordMetadata, Document, Span, remove_overlaps_map};

/// harper-ls's `maxFileLength` default, in bytes.
pub const MAX_TEXT_BYTES: usize = 120_000;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Found {
    pub rule: String,
    pub kind: String,
    pub message: String,
    /// Char (code point) offsets into the checked text.
    pub start: usize,
    pub end: usize,
    pub priority: u8,
    /// The lint's own suggested replacement texts, in Harper's order.
    pub suggestions: Vec<String>,
    /// What harper-ls offered as code actions for this diagnostic (see the module docs).
    pub replacements: Vec<String>,
}

pub fn parse_dialect(name: &str) -> Option<Dialect> {
    match name.trim().to_ascii_lowercase().as_str() {
        "american" | "us" | "en-us" | "en_us" => Some(Dialect::American),
        "british" | "uk" | "gb" | "en-gb" | "en_gb" => Some(Dialect::British),
        "canadian" | "ca" | "en-ca" | "en_ca" => Some(Dialect::Canadian),
        "australian" | "au" | "en-au" | "en_au" => Some(Dialect::Australian),
        "indian" | "in" | "en-in" | "en_in" => Some(Dialect::Indian),
        _ => None,
    }
}

pub struct Engine {
    dialect: Dialect,
    disabled: BTreeSet<String>,
    enabled: BTreeSet<String>,
    words: BTreeSet<String>,
    curated: Option<FlatConfig>,
    dict: Arc<MergedDictionary>,
    linter: LintGroup,
}

impl Engine {
    pub fn new(
        dialect: Dialect,
        disabled: impl IntoIterator<Item = String>,
        enabled: impl IntoIterator<Item = String>,
    ) -> Self {
        let mut e = Engine {
            dialect,
            disabled: disabled.into_iter().collect(),
            enabled: enabled.into_iter().collect(),
            words: BTreeSet::new(),
            curated: None,
            dict: Arc::new(MergedDictionary::new()),
            linter: LintGroup::empty(),
        };
        e.rebuild();
        e
    }

    /// Every rule the curated group knows, sorted.
    pub fn rules(&self) -> Vec<String> {
        let mut out: Vec<String> = self.linter.iter_keys().map(str::to_owned).collect();
        out.sort();
        out
    }

    pub fn words(&self) -> Vec<String> {
        self.words.iter().cloned().collect()
    }

    /// Add words; returns how many were new. Rebuilds the linter only when something changed
    /// (as harper-ls did when its merged dictionary changed).
    pub fn add_words(&mut self, words: impl IntoIterator<Item = String>) -> usize {
        let before = self.words.len();
        for w in words {
            let w = w.trim();
            if !w.is_empty() {
                self.words.insert(w.to_owned());
            }
        }
        let added = self.words.len() - before;
        if added > 0 {
            self.rebuild();
        }
        added
    }

    /// Replace the personal words with `words`.
    pub fn set_words(&mut self, words: impl IntoIterator<Item = String>) {
        let new: BTreeSet<String> = words
            .into_iter()
            .map(|w| w.trim().to_owned())
            .filter(|w| !w.is_empty())
            .collect();
        if new != self.words {
            self.words = new;
            self.rebuild();
        }
    }

    /// Replace the personal words with the lines of `path`; a missing file means none.
    pub fn load_dictionary_file(&mut self, path: &Path) -> std::io::Result<usize> {
        let text = match std::fs::read_to_string(path) {
            Ok(t) => t,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
            Err(e) => return Err(e),
        };
        self.set_words(text.lines().map(str::to_owned));
        Ok(self.words.len())
    }

    pub fn set_disabled_rules(&mut self, rules: impl IntoIterator<Item = String>) {
        self.disabled = rules.into_iter().collect();
        self.apply_config();
    }

    /// Rebuild the linter from scratch: the dictionary changed, or a panic may have left
    /// its caches half-written.
    pub fn rebuild(&mut self) {
        let mut user = MutableDictionary::new();
        let meta = DictWordMetadata {
            dialects: DialectFlags::from_dialect(self.dialect),
            ..Default::default()
        };
        user.extend_words(
            self.words
                .iter()
                .map(|w| (w.chars().collect::<Vec<char>>(), meta.clone())),
        );
        let mut dict = MergedDictionary::new();
        dict.add_dictionary(FstDictionary::curated());
        dict.add_dictionary(Arc::new(user));
        self.dict = Arc::new(dict);
        self.linter = LintGroup::new_curated(self.dict.clone(), self.dialect);
        self.curated = Some(self.linter.config.clone());
        self.apply_config();
        release_freed_memory();
    }

    fn apply_config(&mut self) {
        // new_curated carries the curated config; reset to it, then apply Glance's choices.
        if let Some(c) = &self.curated {
            self.linter.config = c.clone();
        }
        for r in &self.enabled {
            self.linter.config.set_rule_enabled(r, true);
        }
        for r in &self.disabled {
            self.linter.config.set_rule_enabled(r, false);
        }
    }

    pub fn check(&mut self, text: &str) -> Vec<Found> {
        if text.len() > MAX_TEXT_BYTES || text.is_empty() {
            return Vec::new();
        }
        let document = Document::new(text, &PlainEnglish, self.dict.as_ref());
        let source = document.get_source();

        let mut organized: BTreeMap<String, Vec<Lint>> = self.linter.organized_lints(&document);
        // The code-action view: every lint, before overlap removal, by priority (stable).
        let mut all: Vec<Lint> = organized.values().flatten().cloned().collect();
        all.sort_by_key(|l| l.priority);
        remove_overlaps_map(&mut organized);

        let mut out = Vec::new();
        for (rule, lints) in &organized {
            for lint in lints {
                let probe = Span::new(lint.span.start, lint.span.start).with_len(1);
                let mut replacements: Vec<String> = Vec::new();
                for other in all.iter().filter(|o| o.span.overlaps_with(probe)) {
                    for s in &other.suggestions {
                        let t = suggestion_text(other, s, source);
                        if !replacements.contains(&t) {
                            replacements.push(t);
                        }
                    }
                }
                out.push(Found {
                    rule: rule.clone(),
                    kind: format!("{:?}", lint.lint_kind),
                    message: lint.message.clone(),
                    start: lint.span.start,
                    end: lint.span.end,
                    priority: lint.priority,
                    suggestions: lint
                        .suggestions
                        .iter()
                        .map(|s| suggestion_text(lint, s, source))
                        .collect(),
                    replacements,
                });
            }
        }
        out
    }
}

/// Building a LintGroup churns through tens of MB of temporaries; glibc keeps them mapped
/// unless asked (measured: ~35 MB of RSS back after a build).
fn release_freed_memory() {
    #[cfg(all(target_os = "linux", target_env = "gnu"))]
    // SAFETY: malloc_trim only returns free heap pages to the OS.
    unsafe {
        libc::malloc_trim(0);
    }
}

/// The text that replaces the lint's span, as harper-ls's quick fix computed it.
fn suggestion_text(lint: &Lint, s: &Suggestion, source: &[char]) -> String {
    match s {
        Suggestion::ReplaceWith(with) => with.iter().collect(),
        Suggestion::Remove => String::new(),
        Suggestion::InsertAfter(with) => {
            let mut t: String = lint.get_str(source);
            t.extend(with.iter());
            t
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn engine() -> Engine {
        Engine::new(Dialect::American, Vec::new(), Vec::new())
    }

    #[test]
    fn finds_a_misspelling_with_char_offsets() {
        let mut e = engine();
        let text = "😀 Teh cat.";
        let found = e.check(text);
        // harper-ls names this lint's rule "The": a rule key that is also its replacement.
        let teh = found.iter().find(|f| f.start == 2).expect("a lint on Teh");
        let chars: Vec<char> = text.chars().collect();
        let word: String = chars[teh.start..teh.end].iter().collect();
        assert_eq!(word, "Teh");
        assert!(teh.replacements.iter().any(|r| r == "The"));
    }

    #[test]
    fn personal_words_are_not_misspelled() {
        let mut e = engine();
        let t = "A Zorblax here.";
        assert!(e.check(t).iter().any(|f| f.rule == "SpellCheck"));
        assert_eq!(e.add_words(["Zorblax".to_owned()]), 1);
        assert!(!e.check(t).iter().any(|f| f.rule == "SpellCheck"));
        assert_eq!(e.add_words(["Zorblax".to_owned()]), 0);
    }

    #[test]
    fn disabled_rules_are_not_reported() {
        let mut e = engine();
        let t = "i think so";
        assert!(
            e.check(t)
                .iter()
                .any(|f| f.rule == "CapitalizePersonalPronouns")
        );
        e.set_disabled_rules(["CapitalizePersonalPronouns".to_owned()]);
        assert!(
            !e.check(t)
                .iter()
                .any(|f| f.rule == "CapitalizePersonalPronouns")
        );
        e.set_disabled_rules(Vec::new());
        assert!(
            e.check(t)
                .iter()
                .any(|f| f.rule == "CapitalizePersonalPronouns")
        );
    }

    #[test]
    fn oversized_and_empty_texts_are_not_linted() {
        let mut e = engine();
        assert!(e.check("").is_empty());
        assert!(e.check(&"Teh ".repeat(MAX_TEXT_BYTES / 4 + 1)).is_empty());
    }

    #[test]
    fn dialects_parse() {
        assert_eq!(parse_dialect("British"), Some(Dialect::British));
        assert_eq!(parse_dialect("en-US"), Some(Dialect::American));
        assert_eq!(parse_dialect("klingon"), None);
    }
}
