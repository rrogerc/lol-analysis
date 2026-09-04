//! Correctly rounded floating-point sums — a port of CPython's `math.fsum`
//! (Shewchuk's partials algorithm, Modules/mathmodule.c). The Python side
//! ranks with the same function, so a geometric mean computed here is the
//! same bits the parent process computes.

/// CPython's partials array: 32 slots on the stack, spilling to the heap
/// only when a sum needs more (Modules/mathmodule.c does the same). Reads
/// and writes stay inside the live length, so the spill is only ever
/// touched once a sum has more than 32 partials.
struct Partials {
    n: usize,
    fixed: [f64; NSTACK],
    spill: Vec<f64>,
}

const NSTACK: usize = 32;

impl Partials {
    #[inline]
    fn new() -> Partials {
        Partials { n: 0, fixed: [0.0; NSTACK], spill: Vec::new() }
    }

    #[inline]
    fn len(&self) -> usize {
        self.n
    }

    #[inline]
    fn get(&self, i: usize) -> f64 {
        if i < NSTACK {
            self.fixed[i]
        } else {
            self.spill[i - NSTACK]
        }
    }

    #[inline]
    fn set(&mut self, i: usize, v: f64) {
        if i < NSTACK {
            self.fixed[i] = v;
        } else {
            self.spill[i - NSTACK] = v;
        }
    }

    #[inline]
    fn truncate(&mut self, i: usize) {
        self.n = i;
    }

    #[inline]
    fn clear(&mut self) {
        self.n = 0;
    }

    #[inline]
    fn push(&mut self, v: f64) {
        if self.n < NSTACK {
            self.fixed[self.n] = v;
        } else {
            let k = self.n - NSTACK;
            if k < self.spill.len() {
                self.spill[k] = v;
            } else {
                self.spill.push(v);
            }
        }
        self.n += 1;
    }
}

pub fn fsum<I: IntoIterator<Item = f64>>(iter: I) -> f64 {
    let mut p = Partials::new();
    let mut special_sum = 0.0f64;
    let mut inf_sum = 0.0f64;
    let mut lo = 0.0f64;
    for mut x in iter {
        let xsave = x;
        let mut i = 0;
        for j in 0..p.len() {
            let mut y = p.get(j);
            if x.abs() < y.abs() {
                std::mem::swap(&mut x, &mut y);
            }
            let hi = x + y;
            let yr = hi - x;
            lo = y - yr;
            if lo != 0.0 {
                p.set(i, lo);
                i += 1;
            }
            x = hi;
        }
        p.truncate(i);
        if x != 0.0 {
            if !x.is_finite() {
                if xsave.is_finite() {
                    panic!("intermediate overflow in fsum");
                }
                if xsave.is_infinite() {
                    inf_sum += xsave;
                }
                special_sum += xsave;
                p.clear();
            } else {
                p.push(x);
            }
        }
    }
    if special_sum != 0.0 {
        if inf_sum.is_nan() {
            panic!("-inf + inf in fsum");
        }
        return special_sum;
    }
    let mut hi = 0.0f64;
    let mut n = p.len();
    if n > 0 {
        n -= 1;
        hi = p.get(n);
        // sum_exact(ps, hi) from the top, stop when the sum becomes inexact
        while n > 0 {
            let x = hi;
            n -= 1;
            let y = p.get(n);
            hi = x + y;
            let yr = hi - x;
            lo = y - yr;
            if lo != 0.0 {
                break;
            }
        }
        // make half-even rounding work across multiple partials
        if n > 0 && ((lo < 0.0 && p.get(n - 1) < 0.0) || (lo > 0.0 && p.get(n - 1) > 0.0)) {
            let y = lo * 2.0;
            let x = hi + y;
            let yr = x - hi;
            if y == yr {
                hi = x;
            }
        }
    }
    hi
}

/// `math.exp(math.fsum(math.log(max(x, 1e-9)) for x in xs) / len(xs))`
pub fn geo_mean(xs: &[f64]) -> f64 {
    let s = fsum(xs.iter().map(|&x| (if x < 1e-9 { 1e-9 } else { x }).ln()));
    (s / xs.len() as f64).exp()
}
