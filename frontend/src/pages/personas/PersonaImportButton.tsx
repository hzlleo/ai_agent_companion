import { useRef, useState } from 'react'
import { Upload } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { useToast } from '@/components/ui/useToast'
import { toApiError } from '@/api/client'
import { useImportPersonaFile, useImportPersonasBatch } from '@/hooks/usePersonasApi'
import { PersonaImportConflictDialog } from './PersonaImportConflictDialog'
import { PersonaImportBatchResultDialog } from './PersonaImportBatchResultDialog'
import type { PersonaImportBatchResult } from '@/types/persona'

/**
 * 人格文件导入按钮（人格列表页挂载）：.json 卡片文件上传（单文件 /
 * 多文件批量）。
 *
 * 单文件：POST /personas/import（multipart）——内容同卡幂等 toast、
 * 名称冲突三选一重问（跳过 / 改名 / 覆盖，文件保留重试）。多文件：
 * POST /personas/import/batch（部分成功形态）——逐文件结果对话框。
 */
export function PersonaImportButton() {
  const toast = useToast()
  const fileInputRef = useRef<HTMLInputElement>(null)
  const importFileMutation = useImportPersonaFile()
  const importBatchMutation = useImportPersonasBatch()
  const [conflictFile, setConflictFile] = useState<File | null>(null)
  const [batchResult, setBatchResult] = useState<PersonaImportBatchResult | null>(null)

  /** 单文件导入（冲突重试时携带策略与期望名）。 */
  const runSingleImport = (
    file: File,
    options: { conflictStrategy?: 'skip' | 'rename' | 'overwrite'; desiredName?: string } = {}
  ): void => {
    importFileMutation.mutate(
      { file, options },
      {
        onSuccess: (result) => {
          setConflictFile(null)
          if (result.imported) {
            toast.success(`人格「${file.name}」已导入`)
          } else if (result.reason === 'identical_content') {
            toast.info('检测到同卡已存在（内容相同，幂等跳过）')
          } else {
            setConflictFile(file)
          }
        },
        onError: (error) => {
          setConflictFile(null)
          toast.error(toApiError(error).message)
        },
      },
    )
  }

  /** 文件选择分发：单文件直入；多文件批量（部分成功形态）。 */
  const handleFilesSelected = (files: File[]): void => {
    if (files.length === 0) {
      return
    }
    if (files.length === 1) {
      const [first] = files
      if (first !== undefined) {
        runSingleImport(first)
      }
      return
    }
    importBatchMutation.mutate(
      { files, strategy: 'skip' },
      {
        onSuccess: (result) => {
          setBatchResult(result)
          toast.info(
            `批量导入完成（成功 ${result.succeeded} / 失败 ${result.failed} / 新导入 ${result.imported_count}）`,
          )
        },
        onError: (error) => {
          toast.error(toApiError(error).message)
        },
      },
    )
  }

  return (
    <>
      <input
        ref={fileInputRef}
        type="file"
        accept=".json,application/json"
        multiple
        className="hidden"
        aria-label="人格卡片 JSON 文件上传"
        onChange={(e) => {
          handleFilesSelected(Array.from(e.target.files ?? []))
          e.target.value = ''
        }}
      />
      <Button
        variant="outline"
        onClick={() => fileInputRef.current?.click()}
        loading={importFileMutation.isPending || importBatchMutation.isPending}
      >
        <Upload className="h-4 w-4" aria-hidden="true" />
        导入人格
      </Button>

      {conflictFile !== null && (
        <PersonaImportConflictDialog
          open
          onOpenChange={() => setConflictFile(null)}
          targetName={conflictFile.name.replace(/\.json$/i, '')}
          loading={importFileMutation.isPending}
          onRename={(newName) => {
            runSingleImport(conflictFile, { conflictStrategy: 'rename', desiredName: newName })
          }}
          onOverwrite={() => {
            runSingleImport(conflictFile, { conflictStrategy: 'overwrite' })
          }}
          onSkip={() => {
            setConflictFile(null)
            toast.info('已跳过（保留现状）')
          }}
        />
      )}

      {batchResult !== null && (
        <PersonaImportBatchResultDialog
          open
          onOpenChange={() => setBatchResult(null)}
          result={batchResult}
        />
      )}
    </>
  )
}
