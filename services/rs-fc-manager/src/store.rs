//! 每台 VM 的记录（<state_dir>/vms/<env_id>.json）：建环境时**先**写记录、再动宿主上的任何东西，所以 fc-manager 不管在
//! 哪一步崩掉，下一次启动（或 ExecStopPost 的 sweep）都知道要清理什么。记录里没有令牌：agent 令牌只在内存里，
//! VM 本来也活不过 fc-manager 的重启。

use std::collections::BTreeMap;
use std::fs;
use std::io::{self, Write};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use crate::spec::{self, VmSpec};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Record {
    pub env_id: String,
    pub owner: String,
    #[serde(default)]
    pub labels: BTreeMap<String, String>,
    pub created_at: String,
    pub slot: u32,
    pub uid: u32,
    pub spec: VmSpec,
    #[serde(default)]
    pub image: String,
    /// firecracker 在宿主上的 pid 与启动时间（jailer 退出后才知道）。
    #[serde(default)]
    pub pid: Option<i32>,
    #[serde(default)]
    pub pid_start: Option<u64>,
}

pub struct Store {
    dir: PathBuf,
}

impl Store {
    pub fn open(state_dir: &std::path::Path) -> io::Result<Store> {
        let dir = state_dir.join("vms");
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&dir)?;
        Ok(Store { dir })
    }

    fn path(&self, env_id: &str) -> PathBuf {
        self.dir.join(format!("{env_id}.json"))
    }

    /// 写到临时文件、fsync、改名、再 fsync 目录：断电也不会留下写了一半的记录。
    pub fn write(&self, rec: &Record) -> io::Result<()> {
        let tmp = self.dir.join(format!("{}.json.tmp", rec.env_id));
        let mut f = fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(&tmp)?;
        f.write_all(&serde_json::to_vec(rec).map_err(io::Error::other)?)?;
        f.sync_all()?;
        fs::rename(&tmp, self.path(&rec.env_id))?;
        fs::File::open(&self.dir)?.sync_all()
    }

    pub fn remove(&self, env_id: &str) -> io::Result<()> {
        let _ = fs::remove_file(self.dir.join(format!("{env_id}.json.tmp")));
        match fs::remove_file(self.path(env_id)) {
            Err(e) if e.kind() != io::ErrorKind::NotFound => Err(e),
            _ => Ok(()),
        }
    }

    /// 所有记录。读不了的记录只剩 env_id（没有 pid），sweep 照样按 jail、cgroup 去清。
    pub fn load_all(&self) -> Vec<(String, Option<Record>)> {
        let mut out: BTreeMap<String, Option<Record>> = BTreeMap::new();
        let Ok(rd) = fs::read_dir(&self.dir) else {
            return vec![];
        };
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().into_owned();
            let Some(id) = name
                .strip_suffix(".json.tmp")
                .or_else(|| name.strip_suffix(".json"))
            else {
                continue;
            };
            if !spec::valid_env_id(id) {
                continue;
            }
            let rec = fs::read(e.path())
                .ok()
                .and_then(|b| serde_json::from_slice::<Record>(&b).ok())
                .filter(|r| r.env_id == id);
            let slot = out.entry(id.to_string()).or_insert(None);
            if slot.is_none() {
                *slot = rec;
            }
        }
        out.into_iter().collect()
    }
}
