from app import reset_line_state, activation_status, activation_pass_counter, poee_start_times

def test_reset_poee_behavior():
    line = "EOL4"
    part = "2098700356"
    part_fail_discounts = {part: 3}

    # Simula modo POEE
    from app import current_mode
    current_mode = "POEE"

    reset_line_state(line, part)

    assert activation_status[line]["active"] is False
    assert activation_pass_counter[line] == 0
    assert poee_start_times.get(line) is None or isinstance(poee_start_times[line], (str, type(None)))
