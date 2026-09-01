# VM148 AUDIT TRAIL — GOVERNED MKFS (suite 8, reprise post-restart)

```text
authorization_source=USER_ED
vm=148
device=/dev/sdb1
filesystem=ext4
label=MAILCOW_DOCKER
uuid=73bf08e6-7a68-49cb-867a-956dbe194bb6

prechecks=PASS
  (hostname=hp-mail boot_id=2fb95573-c01b-4ffc-a05b-bb0ff4e431f7 identique qualification)
  (device_exists=YES is_block_device=YES major_minor=8:17)
  (size=128G parent=sdb  mounted=NO swap=NO filesystem_existing=NO)
  (wipefs_vide=OUI signature=NO lvm=NO mdraid=NO holders=NONE docker_use=NO fstab_use=NO)
decision=ALLOW (grant d7bf5533-a006-4f12-8f0e-6318cbb3a864, session 20260901_224716_19cb92)
execution_channel=STRUCTURED_QGA (argv allowlisté, pas de shell)
execution_result=PASS (mkfs.ext4 -L MAILCOW_DOCKER /dev/sdb1, exit 0)
postchecks=PASS (ext4, label MAILCOW_DOCKER, uuid 73bf08e6, non monté)
capability_consumed=YES
replay=DENIED (2e usage du même grant → DENY)
```

```text
VM148_GOVERNED_MKFS=PASS
```
