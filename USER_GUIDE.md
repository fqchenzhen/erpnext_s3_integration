# 国际阿里云雅加达对象存储操作手册

本手册面向中国管理员，使用的是 International Alibaba Cloud 账号。为方便你在国际控制台中准确找到位置，控制台菜单名保留英文，解释使用中文。

## 一、部署前确认

- 当前轻量应用服务器 + FRP 测试环境按本方案选择 `Development` 或 `Staging`，使用公网 Endpoint `https://oss-ap-southeast-5.aliyuncs.com`，不依赖 ECS Instance RAM Role；隔离测试可使用最小权限 AccessKey。
- 正式环境部署到 Indonesia (Jakarta) ECS 时选择 `Production`，Region ID 为 `ap-southeast-5`，并使用内网 Endpoint `https://oss-ap-southeast-5-internal.aliyuncs.com`。
- 生产 ECS 使用 ECS Instance RAM Role，不在 ERPNext 中保存长期 AccessKey。Docker Compose 容器通过宿主 ECS 的实例元数据服务获取临时凭证，因此容器网络必须能访问 `100.100.100.200`。
- 附件 Bucket 与备份 Bucket 必须分开。
- 两个 Bucket 都是 Private，并开启 Block Public Access。
- 所有下载通过 Frappe `/s3/{key}`，不启用 OSS 公网直链、预签名跳转、CDN 或浏览器直传。

## 二、进入应用向导

1. 用 System Manager 登录 ERPNext。
2. 在 Apps 页面点击 **ERPNext S3 Integration**，系统直接进入 **Object Storage Settings**。也可以使用 Awesome Bar 搜索或直接访问 `/app/object-storage-settings`。
3. 打开 **Setup Assistant / 配置助手** 页签。
4. 助手只显示四个状态区：附件存储、数据库备份、附件归档、归档恢复，并且只突出当前最需要处理的一步。
5. 页面没有 Guided/Advanced 模式。常用状态始终可见；分类规则、生命周期数字和低频备份字段在所属区块内按需展开。
6. ERPNext 用户语言为 `中文（简体）` 时显示中文；语言为 English 时显示英文。

## 三、创建两个 OSS Bucket

### 1. 附件 Bucket

1. 打开 **OSS Console**。
2. 进入 `Buckets > Create Bucket`。
3. Region 选择 `Indonesia (Jakarta)`。
4. Storage Class 选择 `Standard`。
5. ACL 选择 `Private`。
6. 创建一个只存附件的 Bucket，例如使用企业缩写、环境和 attachments 组成名称。

### 2. 备份 Bucket

重复上述步骤，创建另一个只存备份的 Bucket。不要复用附件 Bucket。

### 3. 阻止公共访问

对两个 Bucket 分别执行：

1. 打开 `Permission Control > Access Control List`，确认是 `Private`。
2. 打开 `Block Public Access` 并启用。
3. 检查并移除 public-read ACL、匿名 Bucket Policy、公共 CDN 源站和浏览器直传 CORS 配置。

应用不会自动修改这些 OSS 安全设置，需要管理员在控制台确认。

## 四、创建两个存储配置档

回到 **Object Storage Settings**：

1. 点击 **Start Attachment Setup / 开始附件配置**。首次配置对话框只要求环境、附件 Bucket、可选 Prefix，以及测试 AccessKey 或生产 RAM Role Name。
2. 系统自动填写 Provider、Purpose、Jakarta Region 和 Endpoint。测试环境自动使用：
   - Region：`ap-southeast-5`
   - Use Internal Endpoint：关闭
   - Endpoint URL：`https://oss-ap-southeast-5.aliyuncs.com`
3. 正式 Jakarta ECS 选择 `Production` 后系统改为：
   - Region：`ap-southeast-5`
   - Use Internal Endpoint：启用
   - Endpoint URL：`https://oss-ap-southeast-5-internal.aliyuncs.com`
   - Credential Mode：`ECS Instance RAM Role`
4. Bucket 只在 Profile 中保存；Settings 不重复显示 Bucket。独立 Bucket 的 Object Prefix 默认留空。
5. 在 Backups 页签点击 **Start Backup Setup / 开始备份配置**，用独立 Backup Bucket 重复操作。
6. 点击 **Save and Check / 保存并检查**。系统依次读取 Bucket 的地域、Private、Block Public Access 和冗余类型，运行 Full Test，启用 Profile、关联 Settings，再启用目标功能；任一步失败都会保持 Disabled 并显示准确原因。

AccessKey 模式仅用于隔离的开发环境。Access Key ID 与 Secret 作为 Password 字段加密保存，但生产环境仍应使用 ECS RAM Role。

## 五、生成并绑定最小权限 RAM 策略

### 1. 生成策略

分别打开附件与备份 Profile：

1. 点击 `Alibaba Cloud > Generate RAM Policy`。
2. 点击 `Copy Policy JSON`。
3. 在 **RAM Console** 打开 `Permissions > Policies > Create Policy`。
4. 选择 `Script`，粘贴 JSON，创建策略。

生成的策略不会包含 `oss:*` 或 Bucket 写管理权限。两份策略包含只读 `oss:GetBucketInfo`，附件策略还包含只读 `oss:GetBucketLifecycle`；备份策略额外包含带 prefix Condition 的 `oss:ListObjects`。

### 2. 创建 ECS 角色

1. 在 RAM Console 打开 `Identities > Roles > Create Role`。
2. Trusted Entity 选择 `Alibaba Cloud Service`。
3. Trusted Service 选择 `Elastic Compute Service`。
4. 创建角色，例如 `ERPNextJakartaObjectStorageRole`。
5. 给同一个角色绑定附件策略和备份策略。

### 3. 绑定到 ECS

1. 在 **ECS Console** 选择 `Indonesia (Jakarta)`。
2. 打开 `Instances`，选择 ERPNext 所在实例。
3. 点击 `More > Instance Settings > Bind/Replace RAM Role`。
4. 选择刚创建的角色并确认。

## 六、运行完整读写测试

分别打开两个 Profile，点击 `Test > Run Full Read/Write Test`。测试顺序为：

1. 初始化凭证和客户端。
2. Put 测试对象。
3. Head 并检查大小、Content-Type 和元数据。
4. Get 并逐字节比较内容。
5. Range Get，为生产下载验证 HTTP 206 能力。
6. 按当前功能测试 Put/Get Tags；未启用自动分类时显示 `Skipped`。
7. Backups Profile 测试受 Prefix 限制的 ListObjects；Attachments Profile 显示 `Skipped`。
8. 按当前远端删除配置测试 Delete；未启用时显示 `Skipped` 并提示手工清理测试对象。
9. Restore 必须用真实归档测试对象演练，因此 Full Test 中显示 `Skipped`。

只有状态为 Passed 且配置指纹未变化，Profile 才允许启用。修改 Bucket、Endpoint、凭证模式、RAM Role、AccessKey，或实际 RAM 能力开关后必须重新测试；只勾选 Review 确认框不会让测试过期。

常见错误：

- Authorization：检查 ECS RAM Role 绑定、策略 Action 和 Resource。
- Not Found：检查 Bucket、Region 与 Endpoint。
- Network：确认 ECS 和 Bucket 同在 Jakarta，并使用内网 Endpoint。
- Temporary Error：查看 OSS 服务状态后重试。

## 七、附件、分类与长期冷存储

附件长期冷存储与数据库/文件备份是两套完全独立的生命周期：附件规则只在 **Attachment Bucket** 生效，备份规则只在 **Backup Bucket** 生效。

这里的“附件长期存储”不是把 `Backup` 文件夹再复制一份，也不是生成第二份附件备份；它是让 Attachment Bucket 中同一个对象在时间满足条件后，从 Standard 转换为 IA，再转换为 Archive。File 记录和 `/s3/{key}` 地址不变，进入 Archive 后通过 Object Restore Request 恢复临时可读副本。

1. 在 Settings 打开 **Attachments / 附件**。连接状态、自动分类、Retention Rules 和附件长期冷存储都在这一个页签内。
2. Attachment 是用户通过 Frappe 上传并实时写入 Attachment Bucket 的 File 对象，不是备份压缩包。Private 和 Public 附件都可以是对象存储附件，区别是 Frappe 权限与 Object Key 的 visibility 路径。
3. Retention Classification 只判断附件应该保持热存储还是以后转冷，不决定附件存到哪个 Bucket：
   - `Rule Name` 只是便于管理员识别的名称，不参与匹配。
   - `Scope`、DocType/Field、MIME Type、File Extension 和 Priority 才是匹配条件。
   - 匹配结果写为对象标签，例如 `retention=business-archive`。
4. 默认策略固定为 `unclassified`。没有确定命中规则的附件保持 Standard，不会因为未知类型而自动转冷。
5. 在 Attachment Bucket 打开 `Data Management > Lifecycle > Create Rule`。
6. 规则条件选择对象标签 `retention=business-archive`，不要选择备份 prefix。
7. 配置：
   - 30 天转 IA
   - 365 天转 Archive
   - Cold Archive 关闭
   - Delete 保持关闭（ERPNext 中 `Delete Attachments After (Days)` 为 `0`）
8. 保存后回到 ERPNext 点击 **重新检查**。系统只读验证规则，Lifecycle 缺失或错误不会阻塞普通附件上传下载，只影响“附件归档”状态。

只有确定命中交易/归档规则或经过授权人工覆盖的附件才写入 `business-archive`。`permanent-hot`、`business-online` 和 `unclassified` 标签都不会匹配这条冷存储规则。若某个内容对象被多个 File 共享，只要其中一个 File 是更热策略，对象就保持更热策略。

OSS 生命周期按对象的最后修改时间计算天数，不是按 ERPNext File 的创建时间计算。重新上传生成新对象时，生命周期会从新对象的最后修改时间重新计算。

应用只写对象标签并给出引导，不会修改 OSS Lifecycle。若附件没有转冷，先检查 File 的 `object_tag_status=Passed`、OSS 对象标签和规则是否位于正确的 Attachment Bucket。

## 八、数据库备份与本地文件目录备份

Backups 页签有三个互相独立的内容选项：

1. **Database Backup**：数据库 SQL 压缩备份，默认开启；每天站点时区 02:00 创建并上传。
2. **Local Public Files Folder Backup**：`sites/<site>/public/files` 的本地目录压缩包，默认关闭。
3. **Local Private Files Folder Backup**：`sites/<site>/private/files` 的本地目录压缩包，默认关闭。

当 Attachment Storage 已启用时，新附件直接进入 Attachment Bucket，不写入本地 `private/files` 或 `public/files`。因此 Local Private Files Folder Backup 只备份仍留在服务器本地目录中的文件，不会再复制 Attachment Bucket 里的对象，也不是附件灾备副本。附件灾难保护应另行使用 OSS Versioning、跨地域复制或第二份独立复制。

默认每次成功上传后删除本地临时备份，并在整次上传成功后保留最近 30 个成功的每日恢复点。失败的备份不会触发清理，也不会删除最后一个成功恢复点。

30 个每日恢复点默认保持 Standard，不需要 Backup Lifecycle。若合规要求长期保留，可展开 **Optional OSS Backup Lifecycle** 并手工在 Backup Bucket 创建只匹配备份 Prefix 的规则。应用不会创建或修改 OSS Lifecycle。Backups 页面会显示最近成功时间、大小、预计占用和健康阈值。

## 九、Retention Rules 的判断顺序

在 Settings 的 **Attachments > Attachment Classification > Edit Classification Rules** 中配置规则。入口始终可见。匹配优先级固定为：

1. File 上的 `Retention Override`（授权管理员人工覆盖）
2. DocType + Field
3. DocType
4. MIME Type / File Extension
5. 默认 `unclassified`

策略热度从高到低：

1. `permanent-hot`
2. `business-online`
3. `business-archive`
4. `unclassified`

如果多个 File 共用同一个内容哈希对象，对象标签采用最热策略。对象标签只有 retention、category、application、environment 和 site，不要把客户姓名、证件号或业务内容写入 category。

## 十、配置并演练归档恢复

1. 在 **Archive Restore** 中启用恢复请求。
2. 将业务角色加入 `Allowed Restore Roles`；System Manager 始终可恢复。
3. 默认恢复副本 1 天、Tier 为 Standard、每 15 分钟检查一次。
4. System Notification 默认启用，Email 默认关闭。
5. 使用不含敏感信息的已归档测试附件创建 **Object Restore Request**。备份冷恢复只由 System Manager 按灾备 Runbook 在 OSS Console/管理员工具执行，不复用用户附件恢复请求。
6. 检查状态：`Requested > Restoring > Ready > Expired`。
7. Ready 后通过 Frappe 下载，并验证 Range 请求。
8. 在过期后确认状态变为 Expired。

规则：

- 用户必须同时拥有原 File 的 read 权限和允许恢复的角色。
- 每个对象只能有一个 Requested、Restoring 或 Ready 请求。
- 提交给 OSS 后不能取消。
- Failed 可以在重试上限内回到 Requested。

## 十一、启用生产功能

1. 在 Attachment Profile 和 Backup Profile 中填好 Bucket、凭证/角色、Endpoint 与两项安全确认。
2. 回到 Settings，分别点击 **Save, Test and Enable**；无需手工按“测试、启用 Profile、关联、启用 Settings”的顺序操作。
3. Full Test 失败时功能保持 Disabled。修正按钮指出的具体问题后再次点击即可。
4. 数据库备份默认开启；本地 public/private files 目录只有确实需要时才开启。
5. Retention Rules、附件 Lifecycle、Backup Lifecycle 和 Archive Restore 都不阻塞基础附件或数据库备份。
6. 上传一个小附件并下载。
7. 点击 `Backup > Run Backup Now`。
8. 在 OSS Console 检查两个 Bucket 的对象、prefix、metadata、tags 和生命周期规则。

## 十二、功能验收清单

- 新附件的 File URL 为 `/s3/...`，Object Key 不含原文件名。
- 中文文件名下载时仍是原来的 Unicode 名称。
- 私有 File 无权限用户不能下载。
- Range 请求返回 206、Content-Range 和正确字节。
- 相同内容共享对象，删除一个 File 不删除对象；删除最后引用才异步删除。
- 标签分类符合优先级，共享对象采用最热策略。
- 未命中规则的附件为 `unclassified` 并保持 Standard。
- 数据库备份默认每天 02:00 上传并保留最近 30 个成功每日恢复点；本地公共/私有 files 目录按独立开关上传到 Backup Bucket。
- 已进入 Attachment Bucket 的实时附件不会被 Local Private Files Folder Backup 重复打包。
- 附件标签为 `business-archive` 的对象在 30 天进入 IA、365 天进入 Archive；Cold Archive 与自动删除默认关闭。
- Backup Lifecycle 默认为关闭；如启用，只作用于 Backup Bucket 的备份 prefix，不影响 Attachment Bucket。
- 归档请求完成 Requested / Restoring / Ready / Expired 与失败重试。
- Bucket 始终 Private + Block Public Access，无预签名直链、CDN 或浏览器直传。
