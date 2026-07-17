#![allow(clippy::useless_conversion)]

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyList;

fn bitmap_first_collision(
    chunk_lists: &[Vec<usize>],
    length: usize,
) -> Result<Option<usize>, usize> {
    let words = length.div_ceil(64);
    let mut merged = vec![0u64; words];
    let mut local = vec![0u64; words];
    for indices in chunk_lists {
        for w in local.iter_mut() {
            *w = 0;
        }
        for &idx in indices {
            if idx >= length {
                return Err(idx);
            }
            let word = idx >> 6;
            let bit = 1u64 << (idx & 63);
            if local[word] & bit != 0 {
                return Ok(Some(idx));
            }
            local[word] |= bit;
        }
        for i in 0..words {
            let overlap = merged[i] & local[i];
            if overlap != 0 {
                return Ok(Some(i * 64 + overlap.trailing_zeros() as usize));
            }
        }
        for i in 0..words {
            merged[i] |= local[i];
        }
    }
    Ok(None)
}

fn contiguous_gap(mut ranges: Vec<(usize, usize)>, total: usize) -> Option<usize> {
    ranges.sort_unstable();
    let mut expected = 0usize;
    for (start, stop) in ranges {
        if start != expected {
            return Some(expected);
        }
        if stop < start || stop > total {
            return Some(start);
        }
        expected = stop;
    }
    if expected != total {
        return Some(expected);
    }
    None
}

#[pyfunction]
fn audit_index_bitmap(chunk_lists: Vec<Vec<usize>>, length: usize) -> PyResult<Option<usize>> {
    bitmap_first_collision(&chunk_lists, length).map_err(|idx| {
        PyValueError::new_err(format!(
            "index {} out of range for write-set of length {}",
            idx, length
        ))
    })
}

#[pyfunction]
fn audit_contiguous(ranges: Vec<(usize, usize)>, total: usize) -> Option<usize> {
    contiguous_gap(ranges, total)
}

#[pyfunction]
fn fold_ordered<'py>(
    py: Python<'py>,
    current: Bound<'py, PyAny>,
    sites: Vec<Bound<'py, PyList>>,
    op: &str,
    skip: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    use pyo3::ffi;
    let combine: unsafe extern "C" fn(
        *mut ffi::PyObject,
        *mut ffi::PyObject,
    ) -> *mut ffi::PyObject = match op {
        "+" => ffi::PyNumber_Add,
        "*" => ffi::PyNumber_Multiply,
        "&" => ffi::PyNumber_And,
        "|" => ffi::PyNumber_Or,
        "^" => ffi::PyNumber_Xor,
        "min" | "max" => {
            return fold_minmax(py, current, sites, op == "min", skip);
        }
        other => {
            return Err(PyValueError::new_err(format!(
                "fold_ordered does not handle op {:?}",
                other
            )))
        }
    };
    let mut acc = current;
    let n = sites.first().map_or(0, |s| s.len());
    for j in 0..n {
        for slab in &sites {
            let value = slab.get_item(j)?;
            if value.is(&skip) {
                continue;
            }
            // Safety: both pointers are live borrowed refs under the GIL held
            // by `py`; the protocol call returns a new reference.
            let raw = unsafe { combine(acc.as_ptr(), value.as_ptr()) };
            acc = unsafe { Bound::from_owned_ptr_or_err(py, raw)? };
        }
    }
    Ok(acc)
}

fn fold_minmax<'py>(
    _py: Python<'py>,
    current: Bound<'py, PyAny>,
    sites: Vec<Bound<'py, PyList>>,
    take_smaller: bool,
    skip: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    use pyo3::basic::CompareOp;
    let op = if take_smaller {
        CompareOp::Lt
    } else {
        CompareOp::Gt
    };
    let mut acc = current;
    let n = sites.first().map_or(0, |s| s.len());
    for j in 0..n {
        for slab in &sites {
            let value = slab.get_item(j)?;
            if value.is(&skip) {
                continue;
            }
            if value.rich_compare(&acc, op)?.is_truthy()? {
                acc = value;
            }
        }
    }
    Ok(acc)
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(audit_contiguous, m)?)?;
    m.add_function(wrap_pyfunction!(audit_index_bitmap, m)?)?;
    m.add_function(wrap_pyfunction!(fold_ordered, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{bitmap_first_collision, contiguous_gap};

    fn collision(chunks: &[&[usize]], length: usize) -> Result<Option<usize>, usize> {
        let owned: Vec<Vec<usize>> = chunks.iter().map(|c| c.to_vec()).collect();
        bitmap_first_collision(&owned, length)
    }

    #[test]
    fn empty_and_single_are_disjoint() {
        assert_eq!(collision(&[], 0), Ok(None));
        assert_eq!(collision(&[], 64), Ok(None));
        assert_eq!(collision(&[&[]], 10), Ok(None));
        assert_eq!(collision(&[&[0, 1, 2, 9]], 10), Ok(None));
    }

    #[test]
    fn in_chunk_duplicate_is_the_repeated_index() {
        assert_eq!(collision(&[&[3, 5, 3]], 10), Ok(Some(3)));
        assert_eq!(collision(&[&[0, 0]], 1), Ok(Some(0)));
    }

    #[test]
    fn cross_chunk_collision_returns_first_overlap() {
        assert_eq!(collision(&[&[1, 2, 3], &[7, 3]], 10), Ok(Some(3)));
        assert_eq!(collision(&[&[10, 20], &[30], &[20]], 40), Ok(Some(20)));
    }

    #[test]
    fn disjoint_partition_has_no_collision() {
        let chunks: Vec<Vec<usize>> = (0..8).map(|k| (k * 16..k * 16 + 16).collect()).collect();
        assert_eq!(bitmap_first_collision(&chunks, 128), Ok(None));
    }

    #[test]
    fn word_boundaries_are_handled() {
        assert_eq!(collision(&[&[63], &[64]], 128), Ok(None));
        assert_eq!(collision(&[&[63, 64, 65], &[64]], 128), Ok(Some(64)));
        assert_eq!(collision(&[&[127], &[127]], 128), Ok(Some(127)));
    }

    #[test]
    fn out_of_range_index_is_reported() {
        assert_eq!(collision(&[&[5]], 5), Err(5));
        assert_eq!(collision(&[&[0, 1], &[2, 100]], 10), Err(100));
    }

    #[test]
    fn contiguous_tiling_is_accepted() {
        assert_eq!(contiguous_gap(vec![(0, 3), (3, 8), (8, 10)], 10), None);
        assert_eq!(contiguous_gap(vec![(3, 8), (0, 3), (8, 10)], 10), None);
        assert_eq!(contiguous_gap(vec![], 0), None);
    }

    #[test]
    fn empty_chunks_with_tied_start_are_ordered_deterministically() {
        assert_eq!(contiguous_gap(vec![(0, 1), (0, 0)], 1), None);
        assert_eq!(contiguous_gap(vec![(0, 0), (0, 1)], 1), None);
        assert_eq!(contiguous_gap(vec![(0, 0), (0, 0), (0, 2)], 2), None);
        assert_eq!(contiguous_gap(vec![(0, 2), (2, 2), (2, 5)], 5), None);
    }

    #[test]
    fn contiguous_gap_and_overlap_are_reported() {
        assert_eq!(contiguous_gap(vec![(0, 3), (4, 8)], 8), Some(3));
        assert_eq!(contiguous_gap(vec![(0, 5), (3, 8)], 8), Some(5));
        assert_eq!(contiguous_gap(vec![(0, 3)], 8), Some(3));
        assert_eq!(contiguous_gap(vec![(0, 3), (3, 10)], 8), Some(3));
    }
}
