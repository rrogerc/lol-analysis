//! Set 18 unit drivers: the shape of each ability — how many targets, over
//! how long, what repeats — with every number read from the unit's own
//! calculations and curve rows (the port of tft_kits.py, one file per
//! slice). See `crate::driver::Driver` for the hooks and `crate::fight`
//! for the helpers a driver calls.
//!
//! `DRIVERS` maps a unit's api name to its driver's name (the Python
//! class's), and `with_driver!` turns that name into the type for a
//! generic body.

pub mod a;
pub mod carries;
pub mod fighters;
pub mod frontline;
pub mod frontline2;
pub mod helpers;
pub mod late;

pub use crate::driver::Plain;
pub use a::*;
pub use carries::*;
pub use fighters::*;
pub use frontline::*;
pub use frontline2::*;
pub use late::*;

/// Unit api name → driver name, in tft_kits.DRIVERS order.
pub const DRIVERS: &[(&str, &str)] = &[
    ("TFT18_Ahri", "Ahri"), ("TFT18_Akali", "Akali"), ("TFT18_Alistar", "Alistar"),
    ("TFT18_Alune", "Alune"), ("TFT18_Amumu", "Amumu"), ("TFT18_Aphelios", "Aphelios"),
    ("TFT18_Ashe", "Ashe"), ("TFT18_Azir", "Azir"), ("TFT18_Brambleback", "Brambleback"),
    ("TFT18_Caitlyn", "Caitlyn"), ("TFT18_Camille", "Camille"), ("TFT18_Cassiopeia", "Cassiopeia"),
    ("TFT18_Cinderling", "Cinderling"), ("TFT18_Diana", "Diana"), ("TFT18_Draven", "Draven"),
    ("TFT18_ElderDragon", "ElderDragon"), ("TFT18_Elise", "Elise"), ("TFT18_Ezreal", "Ezreal"),
    ("TFT18_Fiddlesticks", "Fiddlesticks"), ("TFT18_Gnar", "Gnar"), ("TFT18_Gromp", "Gromp"),
    ("TFT18_Hecarim", "Hecarim"), ("TFT18_Ivern", "Ivern"), ("TFT18_Karma", "Karma"),
    ("TFT18_Kayle", "Kayle"), ("TFT18_Kennen", "Kennen"), ("TFT18_KhaZix", "KhaZix"),
    ("TFT18_Kobuko", "Kobuko"), ("TFT18_KogMaw", "KogMaw"), ("TFT18_Krug", "Krug"),
    ("TFT18_LeBlanc", "LeBlanc"), ("TFT18_Leona", "Leona"), ("TFT18_Lillia", "Lillia"),
    ("TFT18_Lux_Base", "Lux"), ("TFT18_Malphite", "Malphite"), ("TFT18_MamaBeak", "MamaBeak"),
    ("TFT18_Maokai", "Maokai"), ("TFT18_MasterYi", "MasterYi"), ("TFT18_Morgana", "Morgana"),
    ("TFT18_Murkwolf", "Murkwolf"), ("TFT18_Nidalee", "Nidalee"), ("TFT18_Ornn", "Ornn"),
    ("TFT18_Pebbles", "Pebbles"), ("TFT18_Rakan", "Rakan"), ("TFT18_Rammus", "Rammus"),
    ("TFT18_RekSai", "RekSai"), ("TFT18_Rengar", "Rengar"), ("TFT18_Scuttlecrab", "Scuttlecrab"),
    ("TFT18_Sejuani", "Sejuani"), ("TFT18_Sentinel", "Sentinel"), ("TFT18_Sett", "Sett"),
    ("TFT18_Shen", "Shen"), ("TFT18_Sivir", "Sivir"), ("TFT18_Soraka", "Soraka"),
    ("TFT18_Taric", "Taric"), ("TFT18_Teemo", "Teemo"), ("TFT18_Tristana", "Tristana"),
    ("TFT18_Varus", "Varus"), ("TFT18_Veigar", "Veigar"), ("TFT18_Vi", "Vi"),
    ("TFT18_Warwick", "Warwick"), ("TFT18_Xayah", "Xayah"), ("TFT18_Yorick", "Yorick"),
    ("TFT18_Yunara", "Yunara"), ("TFT18_Zyra", "Zyra"),
];

/// Every driver name the crate can dispatch, for the module's listing.
pub const NAMES: &[&str] = &[
    "Driver", "Ahri", "Akali", "Alistar", "Alune", "Amumu", "Aphelios", "Ashe", "Azir",
    "Brambleback", "Caitlyn", "Camille", "Cassiopeia", "Cinderling", "Diana", "Draven",
    "ElderDragon", "Elise", "Ezreal", "Fiddlesticks", "Gnar", "Gromp", "Hecarim", "Ivern",
    "Karma", "Kayle", "Kennen", "KhaZix", "Kobuko", "KogMaw", "Krug", "LeBlanc", "Leona",
    "Lillia", "Lux", "Malphite", "MamaBeak", "Maokai", "MasterYi", "Morgana", "Murkwolf",
    "Nidalee", "Ornn", "Pebbles", "Rakan", "Rammus", "RekSai", "Rengar", "Scuttlecrab",
    "Sejuani", "Sentinel", "Sett", "Shen", "Sivir", "Soraka", "Taric", "Teemo", "Tristana",
    "Varus", "Veigar", "Vi", "Warwick", "Xayah", "Yorick", "Yunara", "Zyra",
];

/// Run `$body` with `$D` bound to the driver type named by `$name`
/// (`"Driver"` is the plain base driver: attacks only). `$body` must
/// evaluate to a `PyResult<_>`; an unknown name is a ValueError.
#[macro_export]
macro_rules! with_driver {
    ($name:expr, $D:ident, $body:block) => {{
        macro_rules! arm { ($t:ty) => {{ type $D = $t; $body }} }
        match $name {
            "Driver" => arm!($crate::drivers::Plain),
            "Ahri" => arm!($crate::drivers::Ahri),
            "Akali" => arm!($crate::drivers::Akali),
            "Alistar" => arm!($crate::drivers::Alistar),
            "Alune" => arm!($crate::drivers::Alune),
            "Amumu" => arm!($crate::drivers::Amumu),
            "Aphelios" => arm!($crate::drivers::Aphelios),
            "Ashe" => arm!($crate::drivers::Ashe),
            "Azir" => arm!($crate::drivers::Azir),
            "Brambleback" => arm!($crate::drivers::Brambleback),
            "Caitlyn" => arm!($crate::drivers::Caitlyn),
            "Camille" => arm!($crate::drivers::Camille),
            "Cassiopeia" => arm!($crate::drivers::Cassiopeia),
            "Cinderling" => arm!($crate::drivers::Cinderling),
            "Diana" => arm!($crate::drivers::Diana),
            "Draven" => arm!($crate::drivers::Draven),
            "ElderDragon" => arm!($crate::drivers::ElderDragon),
            "Elise" => arm!($crate::drivers::Elise),
            "Ezreal" => arm!($crate::drivers::Ezreal),
            "Fiddlesticks" => arm!($crate::drivers::Fiddlesticks),
            "Gnar" => arm!($crate::drivers::Gnar),
            "Gromp" => arm!($crate::drivers::Gromp),
            "Hecarim" => arm!($crate::drivers::Hecarim),
            "Ivern" => arm!($crate::drivers::Ivern),
            "Karma" => arm!($crate::drivers::Karma),
            "Kayle" => arm!($crate::drivers::Kayle),
            "Kennen" => arm!($crate::drivers::Kennen),
            "KhaZix" => arm!($crate::drivers::KhaZix),
            "Kobuko" => arm!($crate::drivers::Kobuko),
            "KogMaw" => arm!($crate::drivers::KogMaw),
            "Krug" => arm!($crate::drivers::Krug),
            "LeBlanc" => arm!($crate::drivers::LeBlanc),
            "Leona" => arm!($crate::drivers::Leona),
            "Lillia" => arm!($crate::drivers::Lillia),
            "Lux" => arm!($crate::drivers::Lux),
            "Malphite" => arm!($crate::drivers::Malphite),
            "MamaBeak" => arm!($crate::drivers::MamaBeak),
            "Maokai" => arm!($crate::drivers::Maokai),
            "MasterYi" => arm!($crate::drivers::MasterYi),
            "Morgana" => arm!($crate::drivers::Morgana),
            "Murkwolf" => arm!($crate::drivers::Murkwolf),
            "Nidalee" => arm!($crate::drivers::Nidalee),
            "Ornn" => arm!($crate::drivers::Ornn),
            "Pebbles" => arm!($crate::drivers::Pebbles),
            "Rakan" => arm!($crate::drivers::Rakan),
            "Rammus" => arm!($crate::drivers::Rammus),
            "RekSai" => arm!($crate::drivers::RekSai),
            "Rengar" => arm!($crate::drivers::Rengar),
            "Scuttlecrab" => arm!($crate::drivers::Scuttlecrab),
            "Sejuani" => arm!($crate::drivers::Sejuani),
            "Sentinel" => arm!($crate::drivers::Sentinel),
            "Sett" => arm!($crate::drivers::Sett),
            "Shen" => arm!($crate::drivers::Shen),
            "Sivir" => arm!($crate::drivers::Sivir),
            "Soraka" => arm!($crate::drivers::Soraka),
            "Taric" => arm!($crate::drivers::Taric),
            "Teemo" => arm!($crate::drivers::Teemo),
            "Tristana" => arm!($crate::drivers::Tristana),
            "Varus" => arm!($crate::drivers::Varus),
            "Veigar" => arm!($crate::drivers::Veigar),
            "Vi" => arm!($crate::drivers::Vi),
            "Warwick" => arm!($crate::drivers::Warwick),
            "Xayah" => arm!($crate::drivers::Xayah),
            "Yorick" => arm!($crate::drivers::Yorick),
            "Yunara" => arm!($crate::drivers::Yunara),
            "Zyra" => arm!($crate::drivers::Zyra),
            other => Err(pyo3::exceptions::PyValueError::new_err(
                format!("no driver named {other:?}"))),
        }
    }};
}
