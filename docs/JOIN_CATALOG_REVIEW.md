# Join Catalog 全量审核

审核时间：2026-09-05T15:59:29.132491+00:00。审核身份：assistant-ddl-data-review（助手结合 DDL 与实际数据的工程审核）。
适用快照：dbs_57a4a4c99520477b1c8e。原始 DDL 与快照的 Schema、数据画像及表结构一致；逐边核验固定 SQLite 中的真实等值连接。

## 结论

97 条候选全部完成检查：13 条启用，72 条不纳入默认连接，12 条证据不足暂不启用；未检查条目为 0。
此前的 12 条保留并补全逐边证据，新增新版支护表到案例主档的精确编码关系。
审核完成不等于全部批准。存储层仅 decision=approved 进入检索和默认连接，其他条目使用 rejected，并以 review_category 区分误连、冗余路径和证据不足。顶层 status=reviewed 表示检查完成。

| 审核分类 | 数量 |
|---|---:|
| 不启用：各表独立行号 | 20 |
| 已审暂缓：案例子表为空 | 11 |
| 不默认启用：子表冗余直连 | 45 |
| 启用：业务编码 | 13 |
| 不启用：位置属性不是案例键 | 3 |
| 不启用：同项目不等于同工区 | 1 |
| 不启用：操作人属性不是对象归属 | 3 |
| 已审暂缓：波形归属证据不足 | 1 |

## 使用边界

- 案例默认经 t_caseinfo 主档连接。45 条非空子表直连是有意不选用的冗余路径，不是 45 条已证明不存在的关系；明确指定子表范围属于另一种查询口径。
- 13 条启用关系均不是数据库已声明的外键。唯一端由 DDL 单列主键/唯一键证明；覆盖和重复度仅描述当前 SQLite 快照。
- 新版支护可按精确案例编码关联主档，不代表它替代或应合并旧版支护；旧版 t_support 仍为默认来源，孤立/不规范编码不自动修补。
- 案例编码只能证明同一案例，不能证明多个明细的一一对应。计数去重不能修复所有明细求和扇出；需要先各自聚合/存在性处理，当前计划不支持时应拒绝。
- 12 条暂缓关系需要非空关联数据或应用写入逻辑/明确外键证据；没有把空表的“无重复”当作数据验证通过。
- Harness 原有 SQL 安全、计划一致性和 user_explicit 校验保持有效；目录更新不是绕过这些门禁的授权。

## 关键反例

旧、新支护表按 i_serialid 可连接 44 行，但其中案例编码相同 0 行、不同 44 行。两个表中的序号不能作为跨表共同键。
t_casetimeinfo 到工区的所属关系也不能只按项目编码连接：项目可有多个工区。经案例主档确定工区的 15 条匹配记录，其项目代码与时间表一致。

## 97 条逐项结论

完整数值与 DDL 证据同时保存在 artifacts/text2sql/schema/join_catalog.review.json 的 review_evidence；任何未匹配记录均保留在原数据。

| ID | 左字段 | 右字段 | 结论 | 共同键数 | 连接行数 | 未匹配键（左/右） |
|---|---|---|---|---:|---:|---|
| join_dbcaa23d9f0b3852 | t_activeevent.i_serialid | t_casetimeinfo.i_serialid | 不启用：各表独立行号 | 0 | 0 | 0/18 |
| join_681cf8915c0fd411 | t_activeevent.i_serialid | t_event.i_serialid | 不启用：各表独立行号 | 0 | 0 | 0/91 |
| join_b9312279479269a9 | t_activeevent.i_serialid | t_support.i_serialid | 不启用：各表独立行号 | 0 | 0 | 0/60 |
| join_ccf853473a080233 | t_activeevent.i_serialid | t_supportnew.i_serialid | 不启用：各表独立行号 | 0 | 0 | 0/76 |
| join_bea0918f1b9fdd76 | t_activeevent.i_serialid | t_waveproject.i_serialid | 不启用：各表独立行号 | 0 | 0 | 0/3 |
| join_14b130763bec5244 | t_activeevent.i_serialid | t_waveproperty.i_serialid | 不启用：各表独立行号 | 0 | 0 | 0/0 |
| join_aa5ae290b6731974 | t_activeinfo.c_caseCode | t_activeinfoevent.c_caseCode | 已审暂缓：案例子表为空 | 0 | 0 | 23/0 |
| join_37cdc982cbdce0fd | t_activeinfo.c_caseCode | t_casedesc.c_caseCode | 不默认启用：子表冗余直连 | 23 | 37 | 0/13 |
| join_ee45497288277742 | t_activeinfo.c_caseCode | t_casefile.c_caseCode | 不默认启用：子表冗余直连 | 23 | 37 | 0/16 |
| join_5dd45ba0d50a1e76 | t_activeinfo.c_caseCode | t_caseinfo.c_caseCode | 启用：业务编码 | 22 | 35 | 1/17 |
| join_ba0c6c14f8cc774c | t_activeinfo.c_caseCode | t_casetimeinfo.c_caseCode | 不默认启用：子表冗余直连 | 2 | 2 | 21/14 |
| join_c50aa80a0e8ec1d1 | t_activeinfo.c_caseCode | t_geology.c_caseCode | 不默认启用：子表冗余直连 | 18 | 45 | 5/5 |
| join_61c3a70704a61bec | t_activeinfo.c_caseCode | t_harm.c_caseCode | 不默认启用：子表冗余直连 | 23 | 37 | 0/15 |
| join_83c41d71aa7af4cc | t_activeinfo.c_caseCode | t_rockdesc.c_caseCode | 不默认启用：子表冗余直连 | 21 | 34 | 2/16 |
| join_62b97ee2c05c6c73 | t_activeinfo.c_caseCode | t_stress.c_caseCode | 不默认启用：子表冗余直连 | 23 | 37 | 0/16 |
| join_9fa8b8f669306524 | t_activeinfo.c_caseCode | t_support.c_caseCode | 不默认启用：子表冗余直连 | 13 | 29 | 10/15 |
| join_5a01d86dc27bf57e | t_activeinfo.c_caseCode | t_supportnew.c_caseCode | 不默认启用：子表冗余直连 | 17 | 61 | 6/22 |
| join_da756c789d464d7d | t_activeinfoevent.c_caseCode | t_casedesc.c_caseCode | 已审暂缓：案例子表为空 | 0 | 0 | 0/36 |
| join_bc0a42afe9f35c2d | t_activeinfoevent.c_caseCode | t_casefile.c_caseCode | 已审暂缓：案例子表为空 | 0 | 0 | 0/39 |
| join_559584bef19b1ac2 | t_activeinfoevent.c_caseCode | t_caseinfo.c_caseCode | 已审暂缓：案例子表为空 | 0 | 0 | 0/39 |
| join_95c31e7651ce1f86 | t_activeinfoevent.c_caseCode | t_casetimeinfo.c_caseCode | 已审暂缓：案例子表为空 | 0 | 0 | 0/16 |
| join_4c7ea3d1692dda3b | t_activeinfoevent.c_caseCode | t_geology.c_caseCode | 已审暂缓：案例子表为空 | 0 | 0 | 0/23 |
| join_dce3c99240da6e57 | t_activeinfoevent.c_caseCode | t_harm.c_caseCode | 已审暂缓：案例子表为空 | 0 | 0 | 0/38 |
| join_7a1c9aa8e31c1d1c | t_activeinfoevent.c_caseCode | t_rockdesc.c_caseCode | 已审暂缓：案例子表为空 | 0 | 0 | 0/37 |
| join_68ddd600b219a9ef | t_activeinfoevent.c_caseCode | t_stress.c_caseCode | 已审暂缓：案例子表为空 | 0 | 0 | 0/39 |
| join_c462d737cfb01d5c | t_activeinfoevent.c_caseCode | t_support.c_caseCode | 已审暂缓：案例子表为空 | 0 | 0 | 0/28 |
| join_a8a733be3a59f745 | t_activeinfoevent.c_caseCode | t_supportnew.c_caseCode | 已审暂缓：案例子表为空 | 0 | 0 | 0/39 |
| join_0d68c3cb5e98fd1b | t_casedesc.c_caseCode | t_casefile.c_caseCode | 不默认启用：子表冗余直连 | 36 | 36 | 0/3 |
| join_4e7daaae06e62138 | t_casedesc.c_caseCode | t_caseinfo.c_caseCode | 启用：业务编码 | 35 | 35 | 1/4 |
| join_a3c6f34ca5ab2056 | t_casedesc.c_caseCode | t_casetimeinfo.c_caseCode | 不默认启用：子表冗余直连 | 13 | 13 | 23/3 |
| join_df5edf06d5b5bc33 | t_casedesc.c_caseCode | t_geology.c_caseCode | 不默认启用：子表冗余直连 | 21 | 31 | 15/2 |
| join_5acce0ecbd1e2416 | t_casedesc.c_caseCode | t_harm.c_caseCode | 不默认启用：子表冗余直连 | 35 | 35 | 1/3 |
| join_95cd2527e72b97d9 | t_casedesc.c_caseCode | t_rockdesc.c_caseCode | 不默认启用：子表冗余直连 | 33 | 34 | 3/4 |
| join_8778484a53c6ace2 | t_casedesc.c_caseCode | t_stress.c_caseCode | 不默认启用：子表冗余直连 | 36 | 36 | 0/3 |
| join_4443492b679e8b3d | t_casedesc.c_caseCode | t_support.c_caseCode | 不默认启用：子表冗余直连 | 25 | 52 | 11/3 |
| join_c8309105d7d81292 | t_casedesc.c_caseCode | t_supportnew.c_caseCode | 不默认启用：子表冗余直连 | 19 | 41 | 17/20 |
| join_1b3cb87b57a3a4cb | t_casefile.c_caseCode | t_caseinfo.c_caseCode | 启用：业务编码 | 38 | 38 | 1/1 |
| join_7ef3a6e5abb91fde | t_casefile.c_caseCode | t_casetimeinfo.c_caseCode | 不默认启用：子表冗余直连 | 15 | 15 | 24/1 |
| join_88be6db2d1f0a116 | t_casefile.c_caseCode | t_geology.c_caseCode | 不默认启用：子表冗余直连 | 23 | 35 | 16/0 |
| join_3b01d4afc39f8b7f | t_casefile.c_caseCode | t_harm.c_caseCode | 不默认启用：子表冗余直连 | 38 | 38 | 1/0 |
| join_a2771c6a1e439b23 | t_casefile.c_caseCode | t_rockdesc.c_caseCode | 不默认启用：子表冗余直连 | 36 | 39 | 3/1 |
| join_5774b6416c32d88b | t_casefile.c_caseCode | t_stress.c_caseCode | 不默认启用：子表冗余直连 | 39 | 39 | 0/0 |
| join_39c64b899de13f62 | t_casefile.c_caseCode | t_support.c_caseCode | 不默认启用：子表冗余直连 | 28 | 60 | 11/0 |
| join_15950c7d8706235a | t_casefile.c_caseCode | t_supportnew.c_caseCode | 不默认启用：子表冗余直连 | 22 | 47 | 17/17 |
| join_6d62516878699c7f | t_caseinfo.c_areatCode | t_workarea.c_areatCode | 启用：业务编码 | 7 | 39 | 0/0 |
| join_b8da6c3298d5dd45 | t_caseinfo.c_caseCode | t_casetimeinfo.c_caseCode | 启用：业务编码 | 15 | 15 | 24/1 |
| join_794fd2534fb71d53 | t_caseinfo.c_caseCode | t_geology.c_caseCode | 启用：业务编码 | 22 | 34 | 17/1 |
| join_a99b84f470d3a23b | t_caseinfo.c_caseCode | t_harm.c_caseCode | 启用：业务编码 | 37 | 37 | 2/1 |
| join_eb930c4f5db39a42 | t_caseinfo.c_caseCode | t_rockdesc.c_caseCode | 启用：业务编码 | 35 | 38 | 4/2 |
| join_5df273001f03a83c | t_caseinfo.c_caseCode | t_stress.c_caseCode | 启用：业务编码 | 38 | 38 | 1/1 |
| join_0b17ca08627d9409 | t_caseinfo.c_caseCode | t_support.c_caseCode | 启用：业务编码 | 28 | 60 | 11/0 |
| join_a059519252ebfd68 | t_caseinfo.c_caseCode | t_supportnew.c_caseCode | 启用：业务编码 | 21 | 44 | 18/18 |
| join_3420e31f5d55da1a | t_caseinfo.c_poleBeginNo | t_casetimeinfo.c_poleBeginNo | 不启用：位置属性不是案例键 | 12 | 12 | 26/4 |
| join_fadeb3aa269bb694 | t_caseinfo.c_poleEndNo | t_casetimeinfo.c_poleEndNo | 不启用：位置属性不是案例键 | 13 | 13 | 23/3 |
| join_78d9880306415d74 | t_caseinfo.c_supportNo | t_casetimeinfo.c_supportNo | 不启用：位置属性不是案例键 | 11 | 11 | 22/4 |
| join_6056e1c5ff472d3b | t_casetimeinfo.c_caseCode | t_geology.c_caseCode | 不默认启用：子表冗余直连 | 5 | 7 | 11/18 |
| join_89daa4f3ff862c3b | t_casetimeinfo.c_caseCode | t_harm.c_caseCode | 不默认启用：子表冗余直连 | 15 | 15 | 1/23 |
| join_13451c417b469486 | t_casetimeinfo.c_caseCode | t_rockdesc.c_caseCode | 不默认启用：子表冗余直连 | 15 | 17 | 1/22 |
| join_624918dd50b67cdd | t_casetimeinfo.c_caseCode | t_stress.c_caseCode | 不默认启用：子表冗余直连 | 15 | 15 | 1/24 |
| join_4198aba4220297ef | t_casetimeinfo.c_caseCode | t_support.c_caseCode | 不默认启用：子表冗余直连 | 15 | 44 | 1/13 |
| join_a7fb4bc12adf9232 | t_casetimeinfo.c_caseCode | t_supportnew.c_caseCode | 不默认启用：子表冗余直连 | 3 | 22 | 13/36 |
| join_7aa89af453fa766d | t_casetimeinfo.c_projectCode | t_project.c_projectCode | 启用：业务编码 | 2 | 18 | 0/1 |
| join_6c115f4a4d606fa0 | t_casetimeinfo.c_projectCode | t_workarea.c_projectCode | 不启用：同项目不等于同工区 | 2 | 31 | 0/1 |
| join_34e778fed4dd21f5 | t_casetimeinfo.i_serialid | t_event.i_serialid | 不启用：各表独立行号 | 17 | 17 | 1/74 |
| join_1d41ebc294770fa0 | t_casetimeinfo.i_serialid | t_support.i_serialid | 不启用：各表独立行号 | 0 | 0 | 18/60 |
| join_65d678c3f1964ef9 | t_casetimeinfo.i_serialid | t_supportnew.i_serialid | 不启用：各表独立行号 | 18 | 18 | 0/58 |
| join_393547ccaed7f99f | t_casetimeinfo.i_serialid | t_waveproject.i_serialid | 不启用：各表独立行号 | 3 | 3 | 15/0 |
| join_7381c83f08070227 | t_casetimeinfo.i_serialid | t_waveproperty.i_serialid | 不启用：各表独立行号 | 0 | 0 | 18/0 |
| join_e98f67753b05195f | t_event.i_serialid | t_support.i_serialid | 不启用：各表独立行号 | 54 | 54 | 37/6 |
| join_770928c2e682e95f | t_event.i_serialid | t_supportnew.i_serialid | 不启用：各表独立行号 | 73 | 73 | 18/3 |
| join_460317790e38526a | t_event.i_serialid | t_waveproject.i_serialid | 不启用：各表独立行号 | 3 | 3 | 88/0 |
| join_574d853851056125 | t_event.i_serialid | t_waveproperty.i_serialid | 不启用：各表独立行号 | 0 | 0 | 91/0 |
| join_f6a8b7eb320bd5da | t_geology.c_caseCode | t_harm.c_caseCode | 不默认启用：子表冗余直连 | 23 | 35 | 0/15 |
| join_2deeb68cbaeffea2 | t_geology.c_caseCode | t_rockdesc.c_caseCode | 不默认启用：子表冗余直连 | 23 | 39 | 0/14 |
| join_fd362a4f44bebb29 | t_geology.c_caseCode | t_stress.c_caseCode | 不默认启用：子表冗余直连 | 23 | 35 | 0/16 |
| join_ddcec6daefbf92f5 | t_geology.c_caseCode | t_support.c_caseCode | 不默认启用：子表冗余直连 | 14 | 39 | 9/14 |
| join_6e213beca2f3ead3 | t_geology.c_caseCode | t_supportnew.c_caseCode | 不默认启用：子表冗余直连 | 18 | 72 | 5/21 |
| join_b66005dd0cb8aeef | t_harm.c_caseCode | t_rockdesc.c_caseCode | 不默认启用：子表冗余直连 | 36 | 39 | 2/1 |
| join_4ab2fce388c39528 | t_harm.c_caseCode | t_stress.c_caseCode | 不默认启用：子表冗余直连 | 38 | 38 | 0/1 |
| join_9e2227087f1bfa1f | t_harm.c_caseCode | t_support.c_caseCode | 不默认启用：子表冗余直连 | 28 | 60 | 10/0 |
| join_1b71362f82c2726d | t_harm.c_caseCode | t_supportnew.c_caseCode | 不默认启用：子表冗余直连 | 21 | 46 | 17/18 |
| join_85e5072d9c8fb574 | t_project.c_opCode | t_waveproject.c_opCode | 不启用：操作人属性不是对象归属 | 0 | 0 | 0/0 |
| join_b6f8fce67fdad31c | t_project.c_opCode | t_workarea.c_opCode | 不启用：操作人属性不是对象归属 | 0 | 0 | 0/0 |
| join_f721e0be9cffc333 | t_project.c_projectCode | t_workarea.c_projectCode | 启用：业务编码 | 3 | 7 | 0/0 |
| join_b01bbfdc195e24fa | t_rockdesc.c_caseCode | t_stress.c_caseCode | 不默认启用：子表冗余直连 | 36 | 39 | 1/3 |
| join_a216e93c7aa20f27 | t_rockdesc.c_caseCode | t_support.c_caseCode | 不默认启用：子表冗余直连 | 27 | 67 | 10/1 |
| join_b95351439230c50b | t_rockdesc.c_caseCode | t_supportnew.c_caseCode | 不默认启用：子表冗余直连 | 20 | 48 | 17/19 |
| join_e0138239368a13dc | t_stress.c_caseCode | t_support.c_caseCode | 不默认启用：子表冗余直连 | 28 | 60 | 11/0 |
| join_7c755f187accc5e8 | t_stress.c_caseCode | t_supportnew.c_caseCode | 不默认启用：子表冗余直连 | 22 | 47 | 17/17 |
| join_131caa6fb69d40ba | t_support.c_caseCode | t_supportnew.c_caseCode | 不默认启用：子表冗余直连 | 12 | 42 | 16/27 |
| join_d72faabf377eab97 | t_support.i_serialid | t_supportnew.i_serialid | 不启用：各表独立行号 | 44 | 44 | 16/32 |
| join_ff1ffb68fc2a0a0d | t_support.i_serialid | t_waveproject.i_serialid | 不启用：各表独立行号 | 0 | 0 | 60/3 |
| join_60cb7a42a94e06b2 | t_support.i_serialid | t_waveproperty.i_serialid | 不启用：各表独立行号 | 0 | 0 | 60/0 |
| join_2ba92c2ad6f68b72 | t_supportnew.i_serialid | t_waveproject.i_serialid | 不启用：各表独立行号 | 3 | 3 | 73/0 |
| join_9765ed8c8906d801 | t_supportnew.i_serialid | t_waveproperty.i_serialid | 不启用：各表独立行号 | 0 | 0 | 76/0 |
| join_e8ff1db3f04e2005 | t_waveproject.c_opCode | t_workarea.c_opCode | 不启用：操作人属性不是对象归属 | 0 | 0 | 0/0 |
| join_de07788271776100 | t_waveproject.i_serialid | t_waveproperty.i_serialid | 已审暂缓：波形归属证据不足 | 0 | 0 | 3/0 |

## 验证记录

22 项针对性自动化检查通过。当前索引为 vanna-744fe78e029b，真实 Chroma 集合计数为 18 条 DDL、400 条 documentation、1 条 SQL；索引中恰好包含 13 条 approved 关系，84 条未启用关系均未入索引。目录文件摘要、源语料与索引一致；重建 Schema 工件后逐条审核证据保持不变，快照变更后旧审核失效。原始数据库文件摘要保持不变。本次未运行真实模型端到端准确率评测。
