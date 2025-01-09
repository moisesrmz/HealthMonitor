// Asegurar que el spinner esté oculto al cargar la página
document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("loading-spinner").style.display = "none";
});

// Escuchar el botón para buscar datos
document.getElementById("fetch-data").addEventListener("click", () => {
    const startDate = document.getElementById("start-date").value;
    const endDate = document.getElementById("end-date").value;

    if (!startDate || !endDate) {
        alert("Por favor, selecciona ambas fechas.");
        return;
    }

    // Mostrar el spinner
    document.getElementById("loading-spinner").style.display = "flex";

    fetch('/api/historico', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start_date: startDate, end_date: endDate })
    })
    .then(response => response.json())
    .then(data => {
        // Ocultar el spinner
        document.getElementById("loading-spinner").style.display = "none";

        if (data.error) {
            alert("Error al consultar datos: " + data.error);
        } else {
            populateTable(data);
            updateChart(data);
        }
    })
    .catch(error => {
        console.error('Error:', error);
        // Ocultar el spinner en caso de error
        document.getElementById("loading-spinner").style.display = "none";
    });
});

// Inicializar DataTables
$(document).ready(function () {
    $('#results-table').DataTable({
        autoWidth: false, // Desactiva el ajuste automático de ancho
        scrollX: true, // Habilita desplazamiento horizontal
        columnDefs: [
            { targets: 0, width: "140px" }, // SerialNumber: Incrementar tamaño un 20%
            { targets: 4, width: "80px" }, 
            { targets: 5, width: "80px" },  // FALine: Decrementar tamaño un 20%
            { targets: 6, width: "80px" },  // Tester: Decrementar tamaño un 20%
            { targets: [9, 10], width: "150px" }, // LVResult y HVResult con ancho fijo
            { targets: "_all", className: "dt-head-center dt-body-left" } // Alineación general
        ],
    });
});


// Población de la Tabla
function populateTable(data) {
    const table = $('#results-table').DataTable();
    table.clear();
    data.forEach(row => {
        table.row.add([
            row.SerialNumber,
            row.PartNumber,
            row.TestDate,
            row.TestTime,
            row.Shift,
            row.FALine,
            row.Tester,
            row.TestResult,
            row.Failure,
            `<span class="limited" data-fulltext="${row.LVResult || 'N/A'}">${row.LVResult || 'N/A'}</span>`,
            `<span class="limited" data-fulltext="${row.HVResult || 'N/A'}">${row.HVResult || 'N/A'}</span>`
        ]);
    });
    table.draw();
}

// Función para actualizar el gráfico
function updateChart(data) {
    const falineData = data.reduce((acc, row) => {
        const faline = row.FALine.trim();
        const testResult = row.TestResult.trim().toUpperCase();
        const partNumber = row.PartNumber;

        if (!acc[faline]) {
            acc[faline] = { passed: 0, failed: 0, partNumbers: new Set() };
        }

        if (testResult === "PASS") acc[faline].passed++;
        if (testResult === "FAIL") acc[faline].failed++;
        acc[faline].partNumbers.add(partNumber);

        return acc;
    }, {});

    const sortedFalines = Object.keys(falineData).sort();
    const x = [], y = [], colors = [], text = [];

    sortedFalines.forEach(faline => {
        const { passed, failed, partNumbers } = falineData[faline];
        const total = passed + failed;
        const yieldValue = total > 0 ? (passed / total) * 100 : 0;

        x.push(faline);
        y.push(yieldValue);

        colors.push(yieldValue >= 95 ? "rgba(60,179,113,0.9)" : yieldValue >= 90 ? "rgba(255,223,0,0.9)" : "rgba(255,99,71,0.9)");

        let labelText = `Yield: ${yieldValue.toFixed(2)}%\n<br>Pasa: ${passed}\n<br>Falla: ${failed}`;
        const partNumbersFormatted = [...partNumbers].join("<br>");
        if (partNumbersFormatted) {
            labelText += `\n<br><br>NP:<br>${partNumbersFormatted}`;
        }

        text.push(labelText);
    });

    const layout = {
        title: {
            text: "",
            font: { color: "#00ccff", size: 24 },
        },
        xaxis: {
            title: "",
            color: "white",
            tickfont: { color: "white", size: 12 },
            titlefont: { color: "#00ccff", size: 16 },
        },
        yaxis: {
            title: "Yield (%)",
            range: [0, 100],
            color: "white",
            tickfont: { color: "white", size: 12 },
            titlefont: { color: "#00ccff", size: 16 },
        },
        plot_bgcolor: "#1b1b1b",
        paper_bgcolor: "#1b1b1b",
        font: { color: "white" },
        showlegend: false,
        bargap: 0.3
    };

    const plotData = [{
        x,
        y,
        type: 'bar',
        text,
        textposition: 'inside',
        textfont: { size: 12, color: "white" },
        hovertemplate: '<b>%{x}</b><br>Yield: %{y:.2f}%<extra></extra>',
        marker: {
            color: colors,
            opacity: 0.9
        }
    }];

    Plotly.newPlot("chart", plotData, layout);
}

// Función para descargar datos en formato CSV
document.getElementById("download-csv").addEventListener("click", () => {
    const table = $('#results-table').DataTable();
    const data = table.rows().nodes().toArray();
    let csv = "Serial Number,Part Number,Test Date,Test Time,Shift,FALine,Tester,Test Result,Failure,LVResult,HVResult\n";

    data.forEach((row, index) => {
        const cells = row.querySelectorAll("td");
        const rowData = Array.from(cells).map(cell => {
            let text = cell.textContent || cell.innerText;
            text = text.replace(/"/g, '""');
            text = text.replace(/\n/g, ' ');
            return text.includes(",") ? `"${text}"` : text;
        });

        if (rowData.length === 11) {
            csv += rowData.join(",") + "\n";
        } else {
            console.warn(`Fila con columnas inconsistentes en índice ${index + 1}:`, rowData);
        }
    });

    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "historico.csv";
    a.click();
});
