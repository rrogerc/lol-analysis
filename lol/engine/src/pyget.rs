//! Reading the JSON-shaped dicts Python hands over (kit encodings, item
//! effects, stat sheets) with Python's own conventions: a key that is
//! missing or None is absent, a number may arrive as int or float.

use pyo3::exceptions::{PyKeyError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict};

pub fn get<'py>(d: &Bound<'py, PyDict>, key: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
    Ok(match d.get_item(key)? {
        Some(v) if !v.is_none() => Some(v),
        _ => None,
    })
}

pub fn has(d: &Bound<'_, PyDict>, key: &str) -> PyResult<bool> {
    Ok(get(d, key)?.is_some())
}

pub fn getf(d: &Bound<'_, PyDict>, key: &str, default: f64) -> PyResult<f64> {
    match get(d, key)? {
        Some(v) => v.extract::<f64>(),
        None => Ok(default),
    }
}

pub fn reqf(d: &Bound<'_, PyDict>, key: &str) -> PyResult<f64> {
    match get(d, key)? {
        Some(v) => v.extract::<f64>(),
        None => Err(PyKeyError::new_err(key.to_string())),
    }
}

fn as_int(v: &Bound<'_, PyAny>, key: &str) -> PyResult<i64> {
    if let Ok(i) = v.extract::<i64>() {
        return Ok(i);
    }
    let f: f64 = v.extract()?;
    if f.fract() == 0.0 {
        Ok(f as i64)
    } else {
        Err(PyTypeError::new_err(format!("{key}: expected an integer, got {f}")))
    }
}

pub fn geti(d: &Bound<'_, PyDict>, key: &str, default: i64) -> PyResult<i64> {
    match get(d, key)? {
        Some(v) => as_int(&v, key),
        None => Ok(default),
    }
}

pub fn reqi(d: &Bound<'_, PyDict>, key: &str) -> PyResult<i64> {
    match get(d, key)? {
        Some(v) => as_int(&v, key),
        None => Err(PyKeyError::new_err(key.to_string())),
    }
}

pub fn gets(d: &Bound<'_, PyDict>, key: &str, default: &str) -> PyResult<String> {
    match get(d, key)? {
        Some(v) => v.extract::<String>(),
        None => Ok(default.to_string()),
    }
}

pub fn reqs(d: &Bound<'_, PyDict>, key: &str) -> PyResult<String> {
    match get(d, key)? {
        Some(v) => v.extract::<String>(),
        None => Err(PyKeyError::new_err(key.to_string())),
    }
}

/// Python truthiness of an optional field (`spellblade.get("reapplyOnhit")`).
pub fn truthy(d: &Bound<'_, PyDict>, key: &str) -> PyResult<bool> {
    match get(d, key)? {
        Some(v) => v.is_truthy(),
        None => Ok(false),
    }
}

pub fn getd<'py>(d: &Bound<'py, PyDict>, key: &str) -> PyResult<Option<Bound<'py, PyDict>>> {
    match get(d, key)? {
        Some(v) => Ok(Some(v.cast_into::<PyDict>()?)),
        None => Ok(None),
    }
}

pub fn reqd<'py>(d: &Bound<'py, PyDict>, key: &str) -> PyResult<Bound<'py, PyDict>> {
    getd(d, key)?.ok_or_else(|| PyKeyError::new_err(key.to_string()))
}

/// A list-valued field; missing or None reads as empty.
pub fn getlist<'py>(d: &Bound<'py, PyDict>, key: &str) -> PyResult<Vec<Bound<'py, PyAny>>> {
    match get(d, key)? {
        Some(v) => v.try_iter()?.collect(),
        None => Ok(Vec::new()),
    }
}

pub fn getvecf(d: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<Vec<f64>>> {
    match get(d, key)? {
        Some(v) => Ok(Some(
            v.try_iter()?.map(|x| x?.extract::<f64>()).collect::<PyResult<Vec<f64>>>()?,
        )),
        None => Ok(None),
    }
}

pub fn dict_of<'py>(v: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    Ok(v.cast::<PyDict>()?.clone())
}
