# W3 個人增量報告

## 基本資料

- 週次／組別／成員／Git 版本：W3／第 4 組／Xiang／`b5b8503947038585584ec1051dd62e71f755b94c`
- AWS 區域／帳號末四碼：`us-east-1`／`0383`
- 標籤：`course=yuntech-115-1`、`week=w03`、`group=4`、`owner=Xiang`
- 審查者簽核：待由組員填寫

## 需求與架構選擇

服務部署在預設 VPC 的預設公有子網 `subnet-02474761014050d51`、AZ `us-east-1c`，使用 Amazon Linux 2023 x86_64 AMI `ami-0b2c9d1f3edcfd709` 與 `t3.micro`。Security Group 只允許 Codespace `23.97.62.118/32` 的 TCP 22、80；8080 只由主機上的 loopback 使用。nginx 聽 TCP 80，反向代理到 `127.0.0.1:8080` 的 inspection service。根磁碟使用加密 gp3，DeleteOnTermination 啟用，IMDSv2 設為 required。

## 成本與核准範圍

核准建立的範圍是 1 個專用 Security Group、1 個匯入的 ed25519 key pair、1 台 `t3.micro` EC2。成本來源為執行中的 EC2、gp3 根磁碟，以及執行期間的公有 IPv4。沒有建立 NAT Gateway、Elastic IP、負載平衡器、資料庫或 IAM 資源。

## 部署與重建步驟

1. 在 Codespace 執行 `bash scripts/verify-aws.sh`，確認 Learner Lab 帳號與 `us-east-1`。
2. 核對出口 IPv4 為 `23.97.62.118`，使用 `/32`。
3. 由 `deploy/up.sh` 產生 user data；打包結果為 2393 bytes，部署 commit 為 `b5b8503947038585584ec1051dd62e71f755b94c`。
4. 建立專用 SG、匯入公鑰與 EC2；user data 安裝並啟動 nginx、inspection。
5. 用 SSH 登入後，以 `cloud-init status --wait`、systemd、`ss` 與 curl 讀回服務狀態。
6. T3 完成兩個受控故障並恢復。
7. T4 用 `deploy/down.sh` 回收第一輪，再用同一 commit 的 `deploy/up.sh` 重建第二輪，最後用 `deploy/down.sh --stop` 停止保留。

## 成功測試

主機內檢查結果：

- `cloud-init status --wait`：`status: done`
- `inspection.service`：`active (running)`
- `nginx.service`：`active (running)`
- inspection 監聽：`127.0.0.1:8080`
- nginx 監聽：`0.0.0.0:80`
- `curl -i http://127.0.0.1:8080/health`：HTTP 200
- `curl -i http://127.0.0.1/health`：HTTP 200
- JSON：`status=ok`、`service=inspection`、version 等於部署 commit
- inspection 啟動時間：`2026-09-22T02:30:38Z`

第二輪重建後，外部健康檢查為：

- Public IP：`54.221.144.193`
- `/health`：HTTP 200
- version：`b5b8503947038585584ec1051dd62e71f755b94c`
- 最後狀態：instance `stopped`

五層第一次觀測時間與 T2 early curl 當時未完整記錄，不能由事後結果推回，待補實驗紀錄。

## 拒絕／故障測試

### T3(a)：Security Group 擋住 TCP 80

- 預測：新 HTTP 連線無法通過 SG，curl 應逾時，HTTP status 為 `000`。
- 操作：移除記錄 SG `sg-0736b311069122630` 的 TCP 80、來源 `23.97.62.118/32` 規則；TCP 22 未修改。
- 實際：curl exit `28`，HTTP `000`，約 `8.07` 秒後 timeout。
- 原因：封包在 SG 入站規則被擋住，未到達 nginx。
- 修正：加回完全相同的 TCP 80 `/32` 規則。
- 恢復：HTTP `200`，curl exit `0`。

### T3(b)：inspection service 停止

- 預測：nginx 保持運作，但反向代理找不到 `127.0.0.1:8080`，應回 HTTP 502。
- 操作：在第一台 EC2 `i-0a4b0548ca0ef0663` 執行 `sudo systemctl stop inspection`，沒有停止 nginx。
- 實際：`inspection=inactive`、`nginx=active`；curl exit `0`，HTTP `502`，約 `0.50` 秒。
- 原因：請求已通過 SG 並到達 nginx，但後端 inspection 沒有監聽 8080。
- 修正：執行 `sudo systemctl start inspection`。
- 恢復：HTTP `200`，curl exit `0`。

## 一次請求經過哪些服務與權限檢查

Codespace 以出口 IP `23.97.62.118` 發出 HTTP 請求，經 IGW 與預設公有子網路由到 EC2。SG 只允許該來源的 TCP 80，請求進入 nginx；nginx 再代理到主機 loopback `127.0.0.1:8080`。inspection systemd service 以 `inspection` 使用者執行 Python 服務，回傳 `/health` JSON。若 SG 擋住，請求沒有 HTTP 回應；若 inspection 停止，nginx 仍能回應但回傳 502。

## AI 協助內容、本人驗證與修改

AI 協助建立 `deploy/up.sh`、`deploy/down.sh` 與部署器，加入互動核准、資源 ID 記錄、標籤核對、IMDSv2、加密 gp3、健康檢查與 scoped cleanup。本人在 Codespace 執行公開測試與 AWS 讀回，確認 17 個本地測試通過、T2 健康頁 200、T3 故障結果，以及 T4 回收／停止結果。部署過程曾修正兩個問題：`tag_args` 缺失，以及 AWS key pair 匯入需使用公開金鑰 `fileb://`；回收腳本另修正 EBS／ENI 已不存在時的 NotFound 判定。

## 精確資源 ID、回收與保留

### 第一輪，已回收

- EC2：`i-0a4b0548ca0ef0663`
- 根 EBS：`vol-061b1a6c473f18cc8`
- ENI：`eni-0bcaeca1d6cc94251`
- Security Group：`sg-0736b311069122630`
- key pair：`key-0369c5bc3d652b611`
- 回收方式：以 `.local/resources.json` 的 ID 執行 `deploy/down.sh`。
- 讀回結果：instance、根 EBS、ENI、Security Group、key pair 均確認不存在。

### 第二輪，停止保留到 W4

- EC2：`i-0b9ab5ea4ec8e0b55`
- 根 EBS：`vol-063533a03d3f87ff9`
- ENI：`eni-08809bd346bc24901`
- Security Group：`sg-0123218c87439eacb`
- key pair：`key-0d739b046f5bff554`
- Public IP（停止前）：`54.221.144.193`
- 保留理由：依 W3 要求保留第二輪主機供 W4 使用；目前 instance 已停止，磁碟、SG、ENI、key pair 仍保留。
- 下週處理：先啟動 instance，重新取得 public IP，重新核對 Codespace `/32` 與健康頁。

## 成本觀察與不確定性

第一輪主機執行期間會產生 EC2、gp3 與公有 IPv4 相關費用；停止後不再產生運算費，但 EBS、保留的 ENI／SG／key pair 是否計費及實際金額，依當期 AWS 官方價格與帳號規則確認。未填寫未查證的金額。

## 未測部分／阻塞／下一步

- T2 五層「第一次觀測 UTC」沒有在現場逐項記錄，待教師或組員要求時補做新的可重現觀測。
- T2 early curl 的失敗結果沒有完整保存，不能補填推測值。
- 報告審查者簽核欄待組員填寫。
- 下週 W4 啟動第二輪主機後，重新核對 public IP、SG `/32`、SSH 指紋與 `/health`。
