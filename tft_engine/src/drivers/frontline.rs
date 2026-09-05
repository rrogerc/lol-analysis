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
pub type Kobuko = crate::driver::Unported;
pub type Leona = crate::driver::Unported;
pub type Ornn = crate::driver::Unported;
pub type Rakan = crate::driver::Unported;
pub type RekSai = crate::driver::Unported;
pub type Alistar = crate::driver::Unported;
pub type Elise = crate::driver::Unported;
pub type Scuttlecrab = crate::driver::Unported;
pub type Sejuani = crate::driver::Unported;
pub type Shen = crate::driver::Unported;
pub type Fiddlesticks = crate::driver::Unported;
