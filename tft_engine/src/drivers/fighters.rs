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
pub type Camille = crate::driver::Unported;
pub type Warwick = crate::driver::Unported;
pub type Brambleback = crate::driver::Unported;
pub type Diana = crate::driver::Unported;
pub type Morgana = crate::driver::Unported;
pub type Rengar = crate::driver::Unported;
pub type ElderDragon = crate::driver::Unported;
pub type Murkwolf = crate::driver::Unported;
pub type Kennen = crate::driver::Unported;
pub type MasterYi = crate::driver::Unported;
pub type Gnar = crate::driver::Unported;
