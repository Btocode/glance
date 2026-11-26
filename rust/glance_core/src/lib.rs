//! `glance_core`: Glance's display-free core, for Python.
//!
//! Phase 1 exposes `Grammar`, Harper running in glanced's own process instead of a
//! `harper-ls` child. Every call that touches Harper releases the GIL, holds the engine's
//! mutex (so one `Grammar` is safe to share between threads) and runs under
//! `catch_unwind`: a panic inside Harper becomes `glance_core.GrammarPanic` (a
//! `RuntimeError`), never a dead daemon.
//!
//! Phase 3 adds the privacy functions (`privacy_find_secrets`, `privacy_find_protected`,
//! `privacy_shield`, `privacy_unshield`): the Rust twin of `glance.privacy.redact` and
//! `glance.privacy.entities`, with Python str (code point) offsets.

pub mod engine;
pub mod privacy;

#[cfg(feature = "python")]
mod python {
    use std::panic::{AssertUnwindSafe, catch_unwind};
    use std::path::PathBuf;
    use std::sync::{Mutex, MutexGuard};

    use pyo3::create_exception;
    use pyo3::exceptions::{PyOSError, PyRuntimeError, PyValueError};
    use pyo3::prelude::*;
    use pyo3::types::PyDict;

    use crate::engine::{Engine, Found, parse_dialect};
    use crate::privacy;

    create_exception!(glance_core, GrammarPanic, PyRuntimeError);

    /// Harper's own version (the engine the crate is pinned to).
    const HARPER_VERSION: &str = "3.3.0";

    #[pyclass(module = "glance_core", frozen)]
    struct Grammar {
        engine: Mutex<Engine>,
        dialect: String,
    }

    fn panic_message(p: &(dyn std::any::Any + Send)) -> String {
        if let Some(s) = p.downcast_ref::<&str>() {
            (*s).to_owned()
        } else if let Some(s) = p.downcast_ref::<String>() {
            s.clone()
        } else {
            "panic".to_owned()
        }
    }

    impl Grammar {
        fn lock(&self) -> MutexGuard<'_, Engine> {
            // A panic while holding the lock poisons it; the engine is rebuilt after one.
            self.engine.lock().unwrap_or_else(|p| p.into_inner())
        }

        /// Run `f` on the engine without the GIL; a panic rebuilds the engine and raises.
        fn guarded<T: Send>(
            &self,
            py: Python<'_>,
            f: impl FnOnce(&mut Engine) -> T + Send,
        ) -> PyResult<T> {
            let r = py.detach(|| {
                let mut guard = self.lock();
                let r = catch_unwind(AssertUnwindSafe(|| f(&mut guard)));
                if r.is_err() {
                    let _ = catch_unwind(AssertUnwindSafe(|| guard.rebuild()));
                }
                r.map_err(|p| panic_message(p.as_ref()))
            });
            r.map_err(|m| GrammarPanic::new_err(format!("Harper panicked: {m}")))
        }
    }

    fn found_to_dict<'py>(py: Python<'py>, f: &Found) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(py);
        d.set_item("rule", &f.rule)?;
        d.set_item("kind", &f.kind)?;
        d.set_item("message", &f.message)?;
        d.set_item("start", f.start)?;
        d.set_item("end", f.end)?;
        d.set_item("priority", f.priority)?;
        d.set_item("suggestions", f.suggestions.clone())?;
        d.set_item("replacements", f.replacements.clone())?;
        Ok(d)
    }

    #[pymethods]
    impl Grammar {
        /// `Grammar(dialect="American", disabled_rules=(), enabled_rules=(), words=(),
        /// dictionary=None)`. `dictionary` is a personal dictionary file (one word per line)
        /// merged with `words`.
        #[new]
        #[pyo3(signature = (dialect="American", disabled_rules=Vec::new(),
                            enabled_rules=Vec::new(), words=Vec::new(), dictionary=None))]
        fn new(
            py: Python<'_>,
            dialect: &str,
            disabled_rules: Vec<String>,
            enabled_rules: Vec<String>,
            words: Vec<String>,
            dictionary: Option<PathBuf>,
        ) -> PyResult<Self> {
            let d = parse_dialect(dialect)
                .ok_or_else(|| PyValueError::new_err(format!("unknown dialect: {dialect}")))?;
            let built = py.detach(|| {
                catch_unwind(AssertUnwindSafe(|| -> Result<Engine, std::io::Error> {
                    let mut e = Engine::new(d, disabled_rules, enabled_rules);
                    if let Some(p) = &dictionary {
                        e.load_dictionary_file(p)?;
                    }
                    e.add_words(words);
                    Ok(e)
                }))
            });
            let engine = match built {
                Ok(Ok(e)) => e,
                Ok(Err(e)) => return Err(PyOSError::new_err(e.to_string())),
                Err(p) => {
                    return Err(GrammarPanic::new_err(format!(
                        "Harper panicked: {}",
                        panic_message(p.as_ref())
                    )));
                }
            };
            Ok(Grammar {
                engine: Mutex::new(engine),
                dialect: format!("{d:?}"),
            })
        }

        /// Lints of `text`: dicts with `rule`, `kind`, `message`, `start`, `end` (Python str
        /// indices), `priority`, `suggestions` and `replacements` (see engine.rs).
        fn check<'py>(&self, py: Python<'py>, text: &str) -> PyResult<Vec<Bound<'py, PyDict>>> {
            let text = text.to_owned();
            let found = self.guarded(py, move |e| e.check(&text))?;
            found.iter().map(|f| found_to_dict(py, f)).collect()
        }

        /// Add one word to the personal words; True when it was new.
        fn add_word(&self, py: Python<'_>, word: &str) -> PyResult<bool> {
            let w = word.trim().to_owned();
            if w.is_empty() || w.chars().any(char::is_whitespace) {
                return Err(PyValueError::new_err("a dictionary entry is a single word"));
            }
            Ok(self.guarded(py, move |e| e.add_words([w]))? > 0)
        }

        /// Replace the personal words with the file's lines (a missing file means none).
        fn load_dictionary(&self, py: Python<'_>, path: PathBuf) -> PyResult<usize> {
            self.guarded(py, move |e| e.load_dictionary_file(&path))?
                .map_err(|e| PyOSError::new_err(e.to_string()))
        }

        fn set_words(&self, py: Python<'_>, words: Vec<String>) -> PyResult<()> {
            self.guarded(py, move |e| e.set_words(words))
        }

        fn words(&self, py: Python<'_>) -> PyResult<Vec<String>> {
            self.guarded(py, |e| e.words())
        }

        fn set_disabled_rules(&self, py: Python<'_>, rules: Vec<String>) -> PyResult<()> {
            self.guarded(py, move |e| e.set_disabled_rules(rules))
        }

        /// Every rule name Harper's curated set knows.
        fn rules(&self, py: Python<'_>) -> PyResult<Vec<String>> {
            self.guarded(py, |e| e.rules())
        }

        #[getter]
        fn dialect(&self) -> &str {
            &self.dialect
        }

        /// Test hook: panic inside the guarded section.
        fn _panic_for_test(&self, py: Python<'_>) -> PyResult<()> {
            self.guarded(py, |_| -> () { panic!("requested by _panic_for_test") })
        }
    }

    type Spans = Vec<(usize, usize, &'static str)>;
    type Segment = (usize, usize, usize, usize, bool);

    fn spans(v: Vec<privacy::Span>) -> Spans {
        v.into_iter().map(|s| (s.start, s.end, s.kind)).collect()
    }

    /// `redact.find_secrets` as (start, end, kind) tuples.
    #[pyfunction]
    fn privacy_find_secrets(text: &str) -> Spans {
        spans(privacy::find_secrets(text))
    }

    /// `entities.find_protected` as (start, end, kind) tuples.
    #[pyfunction]
    fn privacy_find_protected(text: &str) -> Spans {
        spans(privacy::find_protected(text))
    }

    /// `redact.shield` as (text, mapping, has_objects, segments).
    #[pyfunction]
    fn privacy_shield(text: &str) -> (String, Vec<(String, String)>, bool, Vec<Segment>) {
        let s = privacy::shield(text);
        (s.text, s.mapping, s.has_objects, s.segments)
    }

    /// `redact.unshield` given the shielded mapping.
    #[pyfunction]
    fn privacy_unshield(text: &str, mapping: Vec<(String, String)>) -> Option<String> {
        privacy::unshield(text, &mapping)
    }

    #[pymodule]
    fn glance_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
        // Entropy sums like the running interpreter's sum() (compensated since 3.12).
        privacy::COMPENSATED_SUM.store(
            m.py().version_info() >= (3, 12),
            std::sync::atomic::Ordering::Relaxed,
        );
        m.add_function(wrap_pyfunction!(privacy_find_secrets, m)?)?;
        m.add_function(wrap_pyfunction!(privacy_find_protected, m)?)?;
        m.add_function(wrap_pyfunction!(privacy_shield, m)?)?;
        m.add_function(wrap_pyfunction!(privacy_unshield, m)?)?;
        m.add("PRIVACY_UNICODE_VERSION", privacy::tables::UNICODE_VERSION)?;
        m.add_class::<Grammar>()?;
        m.add("GrammarPanic", m.py().get_type::<GrammarPanic>())?;
        m.add("__version__", env!("CARGO_PKG_VERSION"))?;
        m.add("HARPER_VERSION", HARPER_VERSION)?;
        Ok(())
    }
}
