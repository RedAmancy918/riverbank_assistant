import SwiftUI

struct TaskListView: View {
    @EnvironmentObject private var store: AppStore
    @State private var showNewTask = false
    @State private var createdTask: RemoteTask?

    var body: some View {
        NavigationStack {
            Group {
                if store.tasks.isEmpty && !store.isLoading {
                    ContentUnavailableView(
                        "暂无任务",
                        systemImage: "tray",
                        description: Text("从 iPhone 或终端提交复杂任务，树莓派会在后台继续执行。")
                    )
                } else {
                    List(store.tasks) { task in
                        NavigationLink(value: task) {
                            TaskRow(task: task)
                        }
                    }
                    .listStyle(.plain)
                }
            }
            .navigationTitle("后台任务")
            .navigationDestination(for: RemoteTask.self) { task in
                TaskDetailView(initialTask: task)
            }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showNewTask = true } label: {
                        Label("新任务", systemImage: "plus")
                    }
                }
            }
            .refreshable { await store.refreshTasks() }
            .sheet(isPresented: $showNewTask) {
                NewTaskView { task in
                    createdTask = task
                    showNewTask = false
                }
            }
            .task {
                await store.refreshTasks(showLoading: true)
                while !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(4))
                    await store.refreshTasks()
                }
            }
        }
    }
}

struct TaskRow: View {
    let task: RemoteTask

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(alignment: .firstTextBaseline) {
                Text(task.title)
                    .font(.headline)
                    .lineLimit(2)
                Spacer()
                StatusBadge(task: task)
            }
            Text(task.statusMessage)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .lineLimit(2)
            if task.isActive {
                ProgressView(value: task.progress)
                    .tint(.cyan)
            }
            Text(Date.taskDate(task.updatedAt))
                .font(.caption)
                .foregroundStyle(.tertiary)
        }
        .padding(.vertical, 6)
    }
}

struct NewTaskView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var prompt = ""
    @State private var kind = "research"
    @State private var submitting = false
    @State private var localError = ""

    let onCreated: (RemoteTask) -> Void

    var body: some View {
        NavigationStack {
            Form {
                Section("任务") {
                    TextField("标题（可选）", text: $title)
                    TextEditor(text: $prompt)
                        .frame(minHeight: 190)
                        .overlay(alignment: .topLeading) {
                            if prompt.isEmpty {
                                Text("描述目标、需要核实的范围、输出格式等。任务会在树莓派后台执行。")
                                    .foregroundStyle(.tertiary)
                                    .padding(.top, 8)
                                    .allowsHitTesting(false)
                            }
                        }
                }
                Section("模式") {
                    Picker("类型", selection: $kind) {
                        Text("调研报告").tag("research")
                        Text("通用任务").tag("general")
                        Text("文件整理").tag("file")
                    }
                    .pickerStyle(.segmented)
                }
                if !localError.isEmpty {
                    Section { Text(localError).foregroundStyle(.red) }
                }
            }
            .navigationTitle("新建任务")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(submitting ? "提交中…" : "提交") {
                        Task { await submit() }
                    }
                    .disabled(submitting || prompt.trimmingCharacters(in: .whitespacesAndNewlines).count < 3)
                }
            }
        }
    }

    private func submit() async {
        submitting = true
        defer { submitting = false }
        do {
            let task = try await store.createTask(title: title, prompt: prompt, kind: kind)
            onCreated(task)
        } catch {
            localError = error.localizedDescription
        }
    }
}

struct TaskDetailView: View {
    @EnvironmentObject private var store: AppStore
    @State private var task: RemoteTask
    @State private var answer = ""
    @State private var localError = ""
    @State private var working = false

    init(initialTask: RemoteTask) {
        _task = State(initialValue: initialTask)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                HStack {
                    StatusBadge(task: task)
                    Spacer()
                    Text("\(Int(task.progress * 100))%")
                        .foregroundStyle(.secondary)
                }
                if task.isActive {
                    ProgressView(value: task.progress).tint(.cyan)
                }
                InfoCard(title: "当前状态", text: task.statusMessage)
                InfoCard(title: "原始任务", text: task.prompt)

                if task.status == "waiting_input" {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("需要你的补充").font(.headline)
                        Text(task.question)
                        TextField("输入补充信息", text: $answer, axis: .vertical)
                            .textFieldStyle(.roundedBorder)
                        Button("提交补充") { Task { await submitAnswer() } }
                            .buttonStyle(.borderedProminent)
                            .tint(.cyan)
                            .disabled(working || answer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                    .padding()
                    .background(.orange.opacity(0.1), in: RoundedRectangle(cornerRadius: 18))
                }

                if !task.resultSummary.isEmpty {
                    InfoCard(title: "执行结果", text: task.resultSummary)
                }
                if !task.error.isEmpty {
                    InfoCard(title: "错误", text: task.error, color: .red)
                }
                if !task.reportId.isEmpty {
                    NavigationLink {
                        ReportDetailView(
                            report: ReportRecord(
                                id: task.reportId,
                                title: task.title,
                                summary: task.resultSummary,
                                filename: task.reportFilename,
                                relativePath: task.reportFilename,
                                sizeBytes: 0,
                                modifiedAt: "",
                                format: "markdown"
                            )
                        )
                    } label: {
                        Label("打开生成的报告", systemImage: "doc.text.magnifyingglass")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.cyan)
                }
                if task.isActive {
                    Button(role: .destructive) {
                        Task { await cancel() }
                    } label: {
                        Label("取消任务", systemImage: "xmark.circle")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                    .disabled(working)
                }
                Text("任务 ID\n\(task.id)")
                    .font(.caption.monospaced())
                    .foregroundStyle(.tertiary)
                    .textSelection(.enabled)
            }
            .padding()
        }
        .navigationTitle(task.title)
        .navigationBarTitleDisplayMode(.inline)
        .task(id: task.id) {
            while !Task.isCancelled && task.isActive {
                try? await Task.sleep(for: .seconds(2))
                do { task = try await store.fetchTask(id: task.id) }
                catch {
                    if !error.isRiverBankCancellation {
                        localError = error.localizedDescription
                    }
                }
            }
        }
        .alert("操作失败", isPresented: .constant(!localError.isEmpty)) {
            Button("好") { localError = "" }
        } message: { Text(localError) }
    }

    private func submitAnswer() async {
        working = true
        defer { working = false }
        do {
            task = try await store.answer(taskID: task.id, answer: answer)
            answer = ""
        } catch {
            if !error.isRiverBankCancellation {
                localError = error.localizedDescription
            }
        }
    }

    private func cancel() async {
        working = true
        defer { working = false }
        do { task = try await store.cancel(taskID: task.id) }
        catch {
            if !error.isRiverBankCancellation {
                localError = error.localizedDescription
            }
        }
    }
}

struct InfoCard: View {
    let title: String
    let text: String
    var color: Color = .primary

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.headline)
            Text(text).foregroundStyle(color).textSelection(.enabled)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 18))
    }
}
