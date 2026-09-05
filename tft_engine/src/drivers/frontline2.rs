//! Drivers of this slice: ported from tft_kits.py (see mod.rs for the list).

#![allow(unused_imports)]
use crate::driver::Driver;
use crate::drivers::helpers::*;
use crate::fight::{Deal, Fight, Sel};
use crate::fx::Form;
use crate::kit::{CalcId, DType, Kit, RowId, Runtime};
use crate::pyf::{pyint, pymax, pymin, pyround};
use crate::spec::UnitSpec;

// Placeholders until the slice is ported: replace each alias with the driver's struct.
pub type Hecarim = crate::driver::Unported;
pub type Krug = crate::driver::Unported;
pub type Vi = crate::driver::Unported;
pub type Amumu = crate::driver::Unported;
pub type Lillia = crate::driver::Unported;
pub type Malphite = crate::driver::Unported;
pub type Sentinel = crate::driver::Unported;
pub type Maokai = crate::driver::Unported;
pub type Taric = crate::driver::Unported;
