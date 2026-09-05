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
    fn max_keeps_the_first_on_ties() {
        assert!(pymax(0.0, -0.0).is_sign_positive());
        assert!(pymax(-0.0, 0.0).is_sign_negative());
        assert_eq!(pymin(3.0, 2.0), 2.0);
        assert_eq!(pyint(2.9), 2);
        assert_eq!(pyint(-2.9), -2);
    }
}
