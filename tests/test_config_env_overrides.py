from proxlb.utils.config_parser import Config, ConfigParser


def test_env_overrides_credentials_and_hosts() -> None:
    data = {"proxmox_api": {"hosts": ["old"], "user": "root@pam", "token_secret": "from-file"}}
    env = {
        "PROXLB_API_HOSTS": "pve1.example.com, pve2.example.com",
        "PROXLB_API_TOKEN_ID": "proxlb",
        "PROXLB_API_TOKEN_SECRET": "from-secret",
        "PROXLB_API_PASS": "",
    }
    cfg = Config(**ConfigParser.apply_env_overrides(data, env))
    assert cfg.proxmox_api.hosts == ["pve1.example.com", "pve2.example.com"]
    assert cfg.proxmox_api.token_id == "proxlb"
    assert cfg.proxmox_api.token_secret == "from-secret"
    assert cfg.proxmox_api.username == "root@pam"
    assert cfg.proxmox_api.password is None


def test_env_overrides_create_section() -> None:
    data = ConfigParser.apply_env_overrides({}, {"PROXLB_API_HOSTS": "h", "PROXLB_API_USER": "u@pve"})
    assert Config(**data).proxmox_api.username == "u@pve"
