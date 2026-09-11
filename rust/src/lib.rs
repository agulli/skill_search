//! Pattern matching for skill-engine.
//!
//! This is deliberately the *only* thing implemented in Rust. The measurements
//! in `RUST.md` show that parsing, ranking and search are not worth porting —
//! the whole 4.18M-skill corpus passes through parsing and scoring in under
//! ninety seconds, and 92% of a search query is spent inside SQLite. What is
//! worth porting is the one operation Python cannot express efficiently:
//!
//!   "given N patterns and a document, which of them matched?"
//!
//! Python's `re` is a backtracking engine. It can answer "does anything match"
//! quickly by combining patterns into one alternation, but asking *which*
//! matched — needed here because IDF weighting is per-pattern — requires named
//! groups, which measured ten times slower than simply looping over the
//! patterns one at a time. There is no arrangement of `re` that does this well.
//!
//! `regex::RegexSet` is exactly that operation: one finite automaton, one
//! linear pass, no backtracking, and it reports every pattern that matched.
//! Combined with real threads (the GIL leaves ten cores idle during an
//! 82-minute categorisation pass) this measured 24-26x, and where most
//! documents match nothing — 99.4% of skills contain no safety pattern — the
//! short-circuiting `is_match` prefilter measured 2,305x.
//!
//! Everything beyond matching stays in Python: weights, thresholds, field
//! scoring, the security-context discounts, the risk levels. Those took four
//! iterations of false-positive work to calibrate against the real corpus, and
//! they are policy rather than computation. Moving them here would put the
//! delicate part behind a compile step for no gain.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rayon::prelude::*;
use regex::RegexSet;

/// A compiled pattern set, reusable across many documents.
///
/// Compilation costs about 23 ms for 287 patterns and is paid once; doing it
/// per call would dominate everything else.
#[pyclass]
struct Matcher {
    set: RegexSet,
    n_patterns: usize,
}

#[pymethods]
impl Matcher {
    /// Compile a pattern set. Patterns are treated as case-insensitive, which
    /// is what every caller in this project wants.
    #[new]
    fn new(patterns: Vec<String>) -> PyResult<Self> {
        let prepared: Vec<String> =
            patterns.iter().map(|p| format!("(?i){p}")).collect();
        // A single bad pattern names itself in the error rather than failing
        // opaquely: with several hundred of them, "invalid regex" alone would
        // be a miserable thing to debug.
        let set = RegexSet::new(&prepared).map_err(|e| {
            PyValueError::new_err(format!("could not compile pattern set: {e}"))
        })?;
        Ok(Self { set, n_patterns: patterns.len() })
    }

    #[getter]
    fn n_patterns(&self) -> usize {
        self.n_patterns
    }

    /// Which patterns match this one document, as pattern indices.
    fn matches(&self, text: &str) -> Vec<usize> {
        self.set.matches(text).into_iter().collect()
    }

    /// Does *any* pattern match? Short-circuits, so this is the cheap gate.
    ///
    /// Worth ~2,300x over the Python equivalent when most documents are
    /// negative, because it stops at the first match and never enumerates.
    fn is_match(&self, text: &str) -> bool {
        self.set.is_match(text)
    }

    /// Which patterns match each document, in parallel across all cores.
    ///
    /// The GIL is released for the duration: this is the whole point, since
    /// Python leaves nine of ten cores idle on the same work.
    fn matches_batch(&self, py: Python<'_>, texts: Vec<String>) -> Vec<Vec<usize>> {
        py.detach(|| {
            texts
                .par_iter()
                .map(|t| self.set.matches(t).into_iter().collect())
                .collect()
        })
    }

    /// The indices of documents where any pattern matches.
    ///
    /// For a caller that only needs to know *which documents are interesting*
    /// — safety inspection, where 99.4% are not — this avoids enumerating
    /// matches for the overwhelming majority.
    fn interesting(&self, py: Python<'_>, texts: Vec<String>) -> Vec<usize> {
        py.detach(|| {
            texts
                .par_iter()
                .enumerate()
                .filter(|(_, t)| self.set.is_match(t))
                .map(|(i, _)| i)
                .collect()
        })
    }
}

/// How many threads the batch methods will use.
#[pyfunction]
fn threads() -> usize {
    rayon::current_num_threads()
}

#[pymodule]
fn skill_engine_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Matcher>()?;
    m.add_function(wrap_pyfunction!(threads, m)?)?;
    Ok(())
}
