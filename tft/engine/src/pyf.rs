//! Python's float semantics where Rust's differ, so a fight computes the
//! same bits the Python engine did.

/// Python `max(a, b)`: keeps the first unless the second is strictly
/// greater (so `max(0.0, -0.0)` is `0.0`, and NaN never wins).
#[inline]
pub fn pymax(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// Python `min(a, b)`: keeps the first unless the second is strictly less.
#[inline]
pub fn pymin(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

/// Python `round(x)`: half to even.
#[inline]
pub fn pyround(x: f64) -> f64 {
    let r = x.round();   // half away from zero
    if (x - x.trunc()).abs() == 0.5 && r % 2.0 != 0.0 {
        r - x.signum()
    } else {
        r
    }
}

/// Python `int(x)` for a finite float: truncation toward zero.
#[inline]
pub fn pyint(x: f64) -> i64 {
    x.trunc() as i64
}

/// Python `sum()` over floats: CPython adds the items with Neumaier
/// compensation (`f_result` plus a running correction), so a plain running
/// total does not reproduce its bits. An empty sum is Python's `0`.
#[inline]
pub fn pysum(vals: impl IntoIterator<Item = f64>) -> f64 {
    let mut f = 0.0f64;
    let mut c = 0.0f64;
    for x in vals {
        let t = f + x;
        if f.abs() >= x.abs() {
            c += (f - t) + x;
        } else {
            c += (x - t) + f;
        }
        f = t;
    }
    f + c
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rounding_is_bankers() {
        assert_eq!(pyround(0.5), 0.0);
        assert_eq!(pyround(1.5), 2.0);
        assert_eq!(pyround(2.5), 2.0);
        assert_eq!(pyround(-0.5), 0.0);
        assert_eq!(pyround(-1.5), -2.0);
        assert_eq!(pyround(2.4), 2.0);
        assert_eq!(pyround(2.6), 3.0);
    }

    #[test]
    fn sum_is_compensated() {
        // CPython: sum([0.022500000000000003] * 7) == 0.15750000000000003,
        // where a running total gives 0.1575.
        let x = 0.022500000000000003;
        assert_eq!(pysum(vec![x; 7]), 0.15750000000000003);
        assert_eq!(pysum(vec![x; 8]), 0.18000000000000002);
        assert_eq!(pysum(Vec::new()), 0.0);
    }

    #[test]
    fn max_keeps_the_first_on_ties() {
        assert!(pymax(0.0, -0.0).is_sign_positive());
        assert!(pymax(-0.0, 0.0).is_sign_negative());
        assert_eq!(pymin(3.0, 2.0), 2.0);
        assert_eq!(pyint(2.9), 2);
        assert_eq!(pyint(-2.9), -2);
    }
}
