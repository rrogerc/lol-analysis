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
pub type Veigar = crate::driver::Unported;
pub type Teemo = crate::driver::Unported;
pub type Zyra = crate::driver::Unported;
pub type Ivern = crate::driver::Unported;
pub type Cinderling = crate::driver::Unported;
pub type KogMaw = crate::driver::Unported;
pub type MamaBeak = crate::driver::Unported;
pub type Azir = crate::driver::Unported;
pub type Nidalee = crate::driver::Unported;
